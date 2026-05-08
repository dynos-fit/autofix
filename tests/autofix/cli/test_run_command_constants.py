"""Constants module shape tests for ARCH-010 (AC-2 / AC-17.a)."""
from __future__ import annotations

from autofix.cli import run_constants
from autofix.workflow import State


def test_default_max_retries() -> None:
    assert run_constants.DEFAULT_MAX_RETRIES == 3
    assert isinstance(run_constants.DEFAULT_MAX_RETRIES, int)


def test_llm_patch_threshold() -> None:
    assert run_constants.LLM_PATCH_THRESHOLD == 0.6
    assert isinstance(run_constants.LLM_PATCH_THRESHOLD, float)


def test_exit_codes() -> None:
    assert run_constants.EXIT_OK == 0
    assert run_constants.EXIT_FAILED == 1
    assert run_constants.EXIT_USAGE_ERROR == 2
    assert run_constants.EXIT_HUMAN_REVIEW == 3


def test_evidence_placeholder_is_64_zeros() -> None:
    assert run_constants.EVIDENCE_PLACEHOLDER == "0" * 64
    assert len(run_constants.EVIDENCE_PLACEHOLDER) == 64


def test_recovery_branch_constants() -> None:
    assert run_constants.RECOVERY_BRANCH_PREFIX == "autofix/pre-fix-snapshot-"
    assert run_constants.RECOVERY_BRANCH_TS_FORMAT == "%Y%m%dT%H%M%SZ"
    assert run_constants.RECOVERY_BRANCH_RETRY_SUFFIXES == tuple(
        f"-{i}" for i in range(1, 10)
    )


def test_state_label_verbose_covers_all_states() -> None:
    assert len(run_constants.STATE_LABEL_VERBOSE) == 9
    assert set(run_constants.STATE_LABEL_VERBOSE.keys()) == set(State)


def test_all_exports() -> None:
    expected = {
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
    }
    assert set(run_constants.__all__) == expected
