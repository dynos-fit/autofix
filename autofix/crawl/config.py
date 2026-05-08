"""Read/write ``.autofix/config.json`` for the crawl driver (ARCH-016).

The crawl reads three additive keys (``mode``, ``budget``,
``version``) from the project-level config file and falls back to
the documented defaults when any key is missing or the file
doesn't exist. Pre-existing keys (``test.command``, ``post_fix``,
etc.) are preserved on writes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from autofix.crawl.crawl_constants import (
    BUDGET_BALANCED,
    BUDGET_CHEAP,
    BUDGET_AGGRESSIVE,
    CONFIG_KEY_BUDGET,
    CONFIG_KEY_MODE,
    CONFIG_VERSION,
    MODE_PR,
    MODE_PREVIEW,
    MODE_COMMIT,
)


_CONFIG_PATH_RELATIVE = ".autofix/config.json"

_DEFAULT_MODE: str = MODE_PREVIEW
_DEFAULT_BUDGET_NAME: str = "balanced"

_VALID_MODES: tuple[str, ...] = (MODE_PREVIEW, MODE_COMMIT, MODE_PR)
_VALID_BUDGETS: tuple[str, ...] = ("cheap", "balanced", "aggressive")

_BUDGET_NAME_TO_TIER = {
    "cheap": BUDGET_CHEAP,
    "balanced": BUDGET_BALANCED,
    "aggressive": BUDGET_AGGRESSIVE,
}


def config_path(root: Path) -> Path:
    return Path(root) / _CONFIG_PATH_RELATIVE


def read_config(root: Path) -> dict:
    """Read the autofix config, returning a dict with at minimum
    ``{"mode": str, "budget": str}`` resolved against defaults.

    Missing file → returns the defaults with a stderr notice telling
    the operator to run ``autofix init``.
    """
    p = config_path(root)
    raw: dict = {}
    if p.is_file():
        try:
            with p.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            print(
                f"autofix: warning: could not parse {p}; using defaults",
                file=sys.stderr,
                flush=True,
            )
            raw = {}
    else:
        print(
            "autofix: no .autofix/config.json found; using preview mode "
            "+ balanced budget. Run `autofix init` to customize.",
            file=sys.stderr,
            flush=True,
        )

    if not isinstance(raw, dict):
        raw = {}

    mode = raw.get(CONFIG_KEY_MODE)
    if mode not in _VALID_MODES:
        mode = _DEFAULT_MODE

    budget = raw.get(CONFIG_KEY_BUDGET)
    if budget not in _VALID_BUDGETS:
        budget = _DEFAULT_BUDGET_NAME

    return {"mode": mode, "budget": budget}


def write_config(
    root: Path,
    *,
    mode: str,
    budget: str,
) -> Path:
    """Write the additive crawl keys to ``.autofix/config.json``.

    Preserves any other keys already present in the file.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"unknown mode: {mode!r}")
    if budget not in _VALID_BUDGETS:
        raise ValueError(f"unknown budget: {budget!r}")

    p = config_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if p.is_file():
        try:
            with p.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}

    existing["version"] = CONFIG_VERSION
    existing[CONFIG_KEY_MODE] = mode
    existing[CONFIG_KEY_BUDGET] = budget
    p.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return p


def resolve_budget_tier(budget_name: str) -> dict:
    """Map a budget name (cheap/balanced/aggressive) to its tier dict."""
    if budget_name not in _BUDGET_NAME_TO_TIER:
        raise ValueError(f"unknown budget: {budget_name!r}")
    return _BUDGET_NAME_TO_TIER[budget_name]


__all__ = [
    "config_path",
    "read_config",
    "write_config",
    "resolve_budget_tier",
]
