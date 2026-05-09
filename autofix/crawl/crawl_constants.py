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
#
# Centrality (incoming-dependency count) was removed because it
# required language-specific import-graph walking and broke the
# "any-file" property — the crawler now hands ANY tracked file to
# downstream analyzers regardless of language. Recency + churn are
# both git-only signals and language-agnostic.

RELEVANCE_WEIGHT_RECENCY: float = 0.6
RELEVANCE_WEIGHT_CHURN: float = 0.4

# Fallback score used when the git_log adapter reports an empty
# repo (non-git tree, missing log) — each subscore defaults to this
# value so the weighted sum lands at neutral.
NON_GIT_FALLBACK_SCORE: float = 0.5

# Tunables for the recency / churn subscores. Pinned here so the
# no-magic-numbers grep test catches inline literals.
RECENCY_DECAY_DAYS: float = 7.0
CHURN_CAP_COMMITS: int = 10


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


# --- File-classifier scoring tunables (task-20260508-002) ------------------

# Additive boost applied to entrypoint-class files when scoring.
ENTRYPOINT_BOOST: float = 0.15

# Multiplicative downrank applied to low-value classes
# (vendor/generated/build_output/cache/lockfile/binary). Values <1.0
# move scores toward zero.
LOW_VALUE_CLASS_PENALTY: float = 0.5

# Multiplicative downrank applied to files exceeding
# MAX_RELEVANT_FILE_BYTES. Values <1.0 move scores toward zero.
OVERSIZE_FILE_PENALTY: float = 0.7

# Soft byte cap for "relevant" source files. Files larger than this
# are penalized via OVERSIZE_FILE_PENALTY when the oversize flag is on.
MAX_RELEVANT_FILE_BYTES: int = 200_000

# Hop budget specifically for entrypoint-rooted bundle expansion
# (deeper than the generic MAX_BUNDLE_HOPS).
MAX_BUNDLE_HOPS_ENTRYPOINT: int = 2

# Sort priority for class-based expansion. Lower rank = higher
# priority. Junk-sink classes get rank 99 so they sort to the back
# AND are filtered out by callers that drop rank>=99.
CLASS_EXPANSION_PRIORITY: dict = {
    "source": 0,
    "entrypoint": 1,
    "test": 2,
    "config": 3,
    "docs": 4,
    "unknown": 5,
    "lockfile": 99,
    "binary": 99,
    "vendor": 99,
    "generated": 99,
    "build_output": 99,
    "cache": 99,
}

# Reasons a budget tier was hit while picking bundles. Recorded in
# the ledger so operators can see *why* the picker stopped.
BUDGET_HIT_REASON_FILES: str = "files"
BUDGET_HIT_REASON_BYTES: str = "bytes"
BUDGET_HIT_REASON_HOPS: str = "hops"
BUDGET_HIT_REASON_NONE: str = "none"


__all__ = [
    "STALENESS_HORIZON_HOURS",
    "HUB_SATURATION_WINDOW_HOURS",
    "MAX_HUB_APPEARANCES",
    "MAX_BUNDLE_HOPS",
    "MAX_BUNDLE_FILES",
    "MAX_BUNDLE_BYTES",
    "RELEVANCE_WEIGHT_RECENCY",
    "RELEVANCE_WEIGHT_CHURN",
    "NON_GIT_FALLBACK_SCORE",
    "RECENCY_DECAY_DAYS",
    "CHURN_CAP_COMMITS",
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
    "ENTRYPOINT_BOOST",
    "LOW_VALUE_CLASS_PENALTY",
    "OVERSIZE_FILE_PENALTY",
    "MAX_RELEVANT_FILE_BYTES",
    "MAX_BUNDLE_HOPS_ENTRYPOINT",
    "CLASS_EXPANSION_PRIORITY",
    "BUDGET_HIT_REASON_FILES",
    "BUDGET_HIT_REASON_BYTES",
    "BUDGET_HIT_REASON_HOPS",
    "BUDGET_HIT_REASON_NONE",
]
