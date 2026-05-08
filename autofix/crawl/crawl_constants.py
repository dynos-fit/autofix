"""Constants for the continuous-crawl subsystem (ARCH-016).

Side-effect-free module: every numeric and string literal the
crawl modules use lives here. Consumer-module bodies MUST NOT
inline these values — the no-magic-numbers grep test
(``test_crawl_no_magic_numbers.py``) treats inline literals as a
binding contract violation.

Mirrors the discipline established in
:mod:`autofix.cli.run_constants`,
:mod:`autofix.workflow.verify_constants`,
:mod:`autofix.cli.post_fix_constants`.
"""
from __future__ import annotations


# --- Time horizons ---------------------------------------------------------

STALENESS_HORIZON_HOURS: int = 24
HUB_SATURATION_WINDOW_HOURS: int = 24


# --- Hub saturation cap ----------------------------------------------------

MAX_HUB_APPEARANCES: int = 3


# --- Bundle expansion bounds ----------------------------------------------

MAX_BUNDLE_HOPS: int = 1
MAX_BUNDLE_FILES: int = 5
MAX_BUNDLE_BYTES: int = 50_000


# --- Relevance weights (must sum to 1.0) ----------------------------------

RELEVANCE_WEIGHT_RECENCY: float = 0.5
RELEVANCE_WEIGHT_CHURN: float = 0.3
RELEVANCE_WEIGHT_CENTRALITY: float = 0.2

# Fallback score used when the git_log adapter reports an empty
# repo (non-git tree, missing log) — each subscore defaults to this
# value so the weighted sum lands at neutral.
NON_GIT_FALLBACK_SCORE: float = 0.5

# Tunables for the recency / churn / centrality subscores. Pinned
# here so the no-magic-numbers grep test catches inline literals.
RECENCY_DECAY_DAYS: float = 7.0
CHURN_CAP_COMMITS: int = 10
CENTRALITY_CAP_FANOUT: int = 10


# --- Budget tiers ----------------------------------------------------------

BUDGET_CHEAP: dict = {
    "bundles_per_cycle": 1,
    "interval_seconds": 3600,
    "analyzers": ("cheap", "llm:security"),
}
BUDGET_BALANCED: dict = {
    "bundles_per_cycle": 5,
    "interval_seconds": 1800,
    "analyzers": ("cheap", "llm:security", "llm:code-quality"),
}
BUDGET_AGGRESSIVE: dict = {
    "bundles_per_cycle": 20,
    "interval_seconds": 300,
    "analyzers": (
        "cheap",
        "llm:security",
        "llm:code-quality",
        "llm:dead-code",
        "llm:performance",
    ),
}


# --- On-disk ---------------------------------------------------------------

LEDGER_FILENAME: str = "crawl-ledger.jsonl"


# --- Config schema --------------------------------------------------------

CONFIG_KEY_MODE: str = "mode"
CONFIG_KEY_BUDGET: str = "budget"
CONFIG_VERSION: int = 1


# --- Mode enum -------------------------------------------------------------

MODE_PREVIEW: str = "preview"
MODE_COMMIT: str = "commit"
MODE_PR: str = "pr"


__all__ = [
    "STALENESS_HORIZON_HOURS",
    "HUB_SATURATION_WINDOW_HOURS",
    "MAX_HUB_APPEARANCES",
    "MAX_BUNDLE_HOPS",
    "MAX_BUNDLE_FILES",
    "MAX_BUNDLE_BYTES",
    "RELEVANCE_WEIGHT_RECENCY",
    "RELEVANCE_WEIGHT_CHURN",
    "RELEVANCE_WEIGHT_CENTRALITY",
    "NON_GIT_FALLBACK_SCORE",
    "RECENCY_DECAY_DAYS",
    "CHURN_CAP_COMMITS",
    "CENTRALITY_CAP_FANOUT",
    "BUDGET_CHEAP",
    "BUDGET_BALANCED",
    "BUDGET_AGGRESSIVE",
    "LEDGER_FILENAME",
    "CONFIG_KEY_MODE",
    "CONFIG_KEY_BUDGET",
    "CONFIG_VERSION",
    "MODE_PREVIEW",
    "MODE_COMMIT",
    "MODE_PR",
]
