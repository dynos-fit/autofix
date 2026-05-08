"""Per-scan reset inheritance for ARCH-013 subclasses (AC-21, AC-25)."""
from __future__ import annotations

from autofix.analyzers.llm_judgment._base import (
    LLMJudgmentAnalyzer,
    _PER_SCAN_EVENTS,
    _should_log_event,
)
from autofix.analyzers.llm_judgment.dead_code import DeadCodeJudgmentAnalyzer
from autofix.analyzers.llm_judgment.performance import PerformanceJudgmentAnalyzer


def test_dead_code_inherits_reset_hook() -> None:
    """DeadCodeJudgmentAnalyzer must inherit _reset_per_scan_state classmethod.

    Comparing the underlying function (``__func__``) rather than the bound
    method object — accessing a classmethod through a subclass yields a
    different bound-method per attribute access, but the underlying
    function is shared.
    """
    assert (
        DeadCodeJudgmentAnalyzer._reset_per_scan_state.__func__
        is LLMJudgmentAnalyzer._reset_per_scan_state.__func__
    )


def test_performance_inherits_reset_hook() -> None:
    """PerformanceJudgmentAnalyzer must inherit _reset_per_scan_state classmethod."""
    assert (
        PerformanceJudgmentAnalyzer._reset_per_scan_state.__func__
        is LLMJudgmentAnalyzer._reset_per_scan_state.__func__
    )


def test_reset_clears_state_set_by_dead_code_subclass() -> None:
    """A reset call must clear per-scan state that the dead-code subclass logged."""
    _PER_SCAN_EVENTS.clear()
    _should_log_event("scan-A", "AnalyzerError")
    assert "scan-A" in _PER_SCAN_EVENTS

    DeadCodeJudgmentAnalyzer._reset_per_scan_state()
    assert _PER_SCAN_EVENTS == {}


def test_reset_clears_state_set_by_performance_subclass() -> None:
    """A reset call must clear per-scan state that the performance subclass logged."""
    _PER_SCAN_EVENTS.clear()
    _should_log_event("scan-B", "AnalyzerUnavailable")
    assert "scan-B" in _PER_SCAN_EVENTS

    PerformanceJudgmentAnalyzer._reset_per_scan_state()
    assert _PER_SCAN_EVENTS == {}
