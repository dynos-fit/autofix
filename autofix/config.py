"""Configuration management for the autofix system.

Provides parse_interval, config_show, config_set, and resolve_config.
Merges defaults.py values with per-repo .autofix/config.json overrides.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

from autofix import defaults

# ---------------------------------------------------------------------------
# Supported configuration keys and their expected types
# ---------------------------------------------------------------------------

SUPPORTED_KEYS: set[str] = {
    "max_files",
    "interval",
    "max_findings",
    "scan_timeout",
    "llm_timeout",
    "min_confidence",
    "max_open_prs",
    "max_prs_per_day",
    "review_model",
    "dry_run",
}

# Map from config key -> defaults.py attribute name
_DEFAULTS_MAP: Dict[str, str] = {
    "max_files": "LLM_REVIEW_MAX_FILES",
    "interval": None,  # no default in defaults.py; use a hardcoded fallback
    "max_findings": "MAX_FINDINGS_ENTRIES",
    "scan_timeout": "SCAN_TIMEOUT_SECONDS",
    "llm_timeout": "LLM_INVOCATION_TIMEOUT",
    "min_confidence": "MIN_FINDING_CONFIDENCE",
    "max_open_prs": "MAX_OPEN_PRS",
    "max_prs_per_day": "MAX_PRS_PER_DAY",
    "review_model": None,
    "dry_run": None,
}

_INTERVAL_DEFAULT = "30m"
_REVIEW_MODEL_DEFAULT = "default"
_DRY_RUN_DEFAULT = False

# Keys whose values are integers
_INT_KEYS = {"max_files", "max_findings", "scan_timeout", "llm_timeout", "max_open_prs", "max_prs_per_day"}
# Keys whose values are floats
_FLOAT_KEYS = {"min_confidence"}
# Keys whose values are booleans
_BOOL_KEYS = {"dry_run"}
# Keys whose values stay as strings
_STR_KEYS = {"interval", "review_model"}


# ---------------------------------------------------------------------------
# Result dataclass returned by config_show / config_set
# ---------------------------------------------------------------------------

@dataclass
class ConfigResult:
    """Lightweight result wrapper for config operations."""

    exit_code: int = 0
    output: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# parse_interval
# ---------------------------------------------------------------------------

_INTERVAL_RE = re.compile(r"^(\d+)([mh])$")


def parse_interval(value: str) -> int:
    """Parse an interval string like '15m' or '2h' into seconds.

    Raises ValueError for invalid input.
    """
    if not value:
        raise ValueError("Interval string must not be empty")

    match = _INTERVAL_RE.match(value)
    if match is None:
        raise ValueError(
            f"Invalid interval '{value}'. Expected format: <number>m or <number>h "
            "(e.g. '15m', '2h')"
        )

    amount = int(match.group(1))
    suffix = match.group(2)

    if amount < 0:
        raise ValueError(f"Interval must be non-negative, got {amount}")

    multiplier = 60 if suffix == "m" else 3600
    return amount * multiplier


# ---------------------------------------------------------------------------
# resolve_config  (core merge logic)
# ---------------------------------------------------------------------------

def _build_defaults() -> Dict[str, Any]:
    """Build the defaults dict from defaults.py constants."""
    result: Dict[str, Any] = {}
    for key, attr in _DEFAULTS_MAP.items():
        if attr is not None:
            result[key] = getattr(defaults, attr)
    # Hardcoded fallbacks for keys without a defaults.py mapping
    result.setdefault("interval", _INTERVAL_DEFAULT)
    result.setdefault("review_model", _REVIEW_MODEL_DEFAULT)
    result.setdefault("dry_run", _DRY_RUN_DEFAULT)
    return result


def _read_config_json(root: Path) -> Dict[str, Any]:
    """Read .autofix/config.json, returning an empty dict on missing/invalid file."""
    config_path = root / ".autofix" / "config.json"
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_config(root: Path) -> Dict[str, Any]:
    """Return the merged configuration dict (defaults + per-repo overrides)."""
    merged = _build_defaults()
    overrides = _read_config_json(root)
    for key, value in overrides.items():
        if key in SUPPORTED_KEYS:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# config_show
# ---------------------------------------------------------------------------

def config_show(root: Path, as_json: bool = False) -> ConfigResult:
    """Show the resolved configuration for the current repo.

    Returns a ConfigResult with exit_code 0 on success.
    When as_json is True, output is a JSON string; otherwise human-readable text.
    """
    try:
        merged = resolve_config(root)
    except OSError as exc:
        return ConfigResult(exit_code=1, output="", message=f"Error reading config: {exc}")

    if as_json:
        output = json.dumps(merged, indent=2, sort_keys=True)
    else:
        lines = []
        for key in sorted(merged):
            lines.append(f"{key} = {merged[key]}")
        output = "\n".join(lines)

    return ConfigResult(exit_code=0, output=output)


# ---------------------------------------------------------------------------
# config_set
# ---------------------------------------------------------------------------

def _parse_value(key: str, raw: str) -> Any:
    """Coerce a raw string value to the appropriate Python type for *key*."""
    if key in _INT_KEYS:
        return int(raw)
    if key in _FLOAT_KEYS:
        return float(raw)
    if key in _BOOL_KEYS:
        if raw.lower() in ("true", "1", "yes"):
            return True
        if raw.lower() in ("false", "0", "no"):
            return False
        raise ValueError(f"Invalid boolean value: {raw}")
    # String keys (interval, review_model) -- validate interval format
    if key == "interval":
        parse_interval(raw)  # validates; raises on bad input
    return raw


def config_set(root: Path, key: str, value: str) -> ConfigResult:
    """Set a single config key in .autofix/config.json.

    Returns a ConfigResult with exit_code 0 on success, 1 on validation failure.
    """
    if key not in SUPPORTED_KEYS:
        valid = ", ".join(sorted(SUPPORTED_KEYS))
        return ConfigResult(
            exit_code=1,
            message=f"Unsupported key '{key}'. Valid keys: {valid}",
        )

    try:
        parsed = _parse_value(key, value)
    except (ValueError, TypeError) as exc:
        return ConfigResult(exit_code=1, message=f"Invalid value for '{key}': {exc}")

    config_path = root / ".autofix" / "config.json"
    existing = _read_config_json(root)
    existing[key] = parsed

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        return ConfigResult(exit_code=1, message=f"Error writing config: {exc}")

    return ConfigResult(exit_code=0, output=f"{key} = {parsed}", message=f"Set {key} = {parsed}")
