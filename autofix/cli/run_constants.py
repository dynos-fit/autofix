"""Constants for the autofix run umbrella subcommand (ARCH-010)."""
from __future__ import annotations

from autofix.workflow.state_machine import State

DEFAULT_MAX_RETRIES: int = 3
LLM_PATCH_THRESHOLD: float = 0.6

EXIT_OK: int = 0
EXIT_FAILED: int = 1
EXIT_USAGE_ERROR: int = 2
EXIT_HUMAN_REVIEW: int = 3

EVIDENCE_PLACEHOLDER: str = "0" * 64

RECOVERY_BRANCH_PREFIX: str = "autofix/pre-fix-snapshot-"
RECOVERY_BRANCH_TS_FORMAT: str = "%Y%m%dT%H%M%SZ"
RECOVERY_BRANCH_RETRY_SUFFIXES: tuple[str, ...] = tuple(f"-{i}" for i in range(1, 10))

# When the operator passes --auto-llm without an explicit --analyzers
# set, expand the implicit set to include the cheap analyzer plus every
# LLM judgment analyzer registered in autofix/funnel/pipeline.py. This
# makes "I asked for LLM-driven repair" a single flag instead of a
# multi-analyzer comma-separated string.
DEFAULT_AUTO_LLM_ANALYZERS: tuple[str, ...] = (
    "cheap",
    "llm:security",
    "llm:code-quality",
    "llm:dead-code",
    "llm:performance",
)
LLM_ANALYZER_PREFIX: str = "llm:"

STATE_LABEL_VERBOSE: dict[State, str] = {
    State.SCANNING: "scanning",
    State.TRIAGING: "triaging",
    State.PLANNING: "planning",
    State.APPLYING: "applying",
    State.VERIFYING: "verifying",
    State.RETRY: "retry",
    State.HUMAN_REVIEW: "human-review",
    State.DONE: "done",
    State.FAILED: "failed",
}

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "LLM_PATCH_THRESHOLD",
    "EXIT_OK",
    "EXIT_FAILED",
    "EXIT_USAGE_ERROR",
    "EXIT_HUMAN_REVIEW",
    "EVIDENCE_PLACEHOLDER",
    "RECOVERY_BRANCH_PREFIX",
    "RECOVERY_BRANCH_TS_FORMAT",
    "RECOVERY_BRANCH_RETRY_SUFFIXES",
    "DEFAULT_AUTO_LLM_ANALYZERS",
    "LLM_ANALYZER_PREFIX",
    "STATE_LABEL_VERBOSE",
]
