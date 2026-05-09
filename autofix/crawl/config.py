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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class CrawlerFlags:
    """Frozen feature-flag bundle for the optional crawl subsystems.

    Every flag defaults to ``False`` so a vanilla ``.autofix/config.json``
    (or no config file at all) preserves the pre-task-20260508-002
    behavior bit-for-bit. ``read_crawler_flags`` is the sole IO entry
    point — this class itself is import-pure with zero side effects.

    Configuration keys consulted (all under the top-level ``crawler``
    namespace; missing keys silently fall through to ``False``):

    * ``crawler.scoring.entrypoint_boost``
    * ``crawler.scoring.low_value_class_penalty``
    * ``crawler.scoring.oversize_file_penalty``
    * ``crawler.expansion.class_aware``
    * ``crawler.modes.impact_cone``
    """

    entrypoint_boost: bool = False
    low_value_class_penalty: bool = False
    oversize_file_penalty: bool = False
    class_aware: bool = False
    impact_cone: bool = False


def _coerce_bool(value: object) -> bool:
    """Coerce a config value to ``bool``. Anything not strictly ``True``
    or ``"true"`` (case-insensitive) is treated as ``False``.

    The strict mapping prevents truthy strings like ``"false"`` from
    silently flipping a flag on.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def read_crawler_flags(root: Path) -> CrawlerFlags:
    """Read ``.autofix/config.json`` and return a populated CrawlerFlags.

    Default-off invariant: when the file is missing, malformed, or has
    no ``crawler`` section, returns ``CrawlerFlags()`` (all False). All
    file-IO and JSON-parse errors are swallowed and replaced with the
    safe default — this function MUST NOT raise for any caller input.

    Reads keys:
        crawler.scoring.entrypoint_boost
        crawler.scoring.low_value_class_penalty
        crawler.scoring.oversize_file_penalty
        crawler.expansion.class_aware
        crawler.modes.impact_cone

    Any missing key → False for that flag.
    """
    p = config_path(root)
    if not p.is_file():
        return CrawlerFlags()

    try:
        with p.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return CrawlerFlags()

    if not isinstance(raw, dict):
        return CrawlerFlags()

    crawler = raw.get("crawler")
    if not isinstance(crawler, dict):
        return CrawlerFlags()

    # Hoist into typed locals so mypy can narrow correctly. The
    # previous ternary form (`x if isinstance(x, dict) else {}`)
    # called `crawler.get()` twice and confused mypy's union-attr
    # narrowing across the two calls — see audit-finding cq-001.
    scoring_raw = crawler.get("scoring")
    scoring: dict = scoring_raw if isinstance(scoring_raw, dict) else {}
    expansion_raw = crawler.get("expansion")
    expansion: dict = expansion_raw if isinstance(expansion_raw, dict) else {}
    modes_raw = crawler.get("modes")
    modes: dict = modes_raw if isinstance(modes_raw, dict) else {}

    return CrawlerFlags(
        entrypoint_boost=_coerce_bool(scoring.get("entrypoint_boost", False)),
        low_value_class_penalty=_coerce_bool(
            scoring.get("low_value_class_penalty", False)
        ),
        oversize_file_penalty=_coerce_bool(
            scoring.get("oversize_file_penalty", False)
        ),
        class_aware=_coerce_bool(expansion.get("class_aware", False)),
        impact_cone=_coerce_bool(modes.get("impact_cone", False)),
    )


__all__ = [
    "config_path",
    "read_config",
    "write_config",
    "resolve_budget_tier",
    "CrawlerFlags",
    "read_crawler_flags",
]
