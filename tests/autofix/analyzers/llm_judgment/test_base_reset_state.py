"""Test reset state functionality (AC-10f).

Populate _PER_SCAN_EVENTS, call _reset_per_scan_state(), assert empty.
"""

from __future__ import annotations

import pytest

from autofix.analyzers.llm_judgment._base import LLMJudgmentAnalyzer, _PER_SCAN_EVENTS


def test_reset_per_scan_state_clears_events() -> None:
    """_reset_per_scan_state clears the _PER_SCAN_EVENTS dict."""
    # Populate the state dict
    _PER_SCAN_EVENTS["scan-1"] = {"AnalyzerError", "AnalyzerUnavailable"}
    _PER_SCAN_EVENTS["scan-2"] = {"AnalyzerError"}

    # Verify it's populated
    assert len(_PER_SCAN_EVENTS) == 2
    assert "scan-1" in _PER_SCAN_EVENTS
    assert "scan-2" in _PER_SCAN_EVENTS

    # Reset
    LLMJudgmentAnalyzer._reset_per_scan_state()

    # Verify it's empty
    assert len(_PER_SCAN_EVENTS) == 0
    assert _PER_SCAN_EVENTS == {}


def test_reset_per_scan_state_on_empty_dict() -> None:
    """_reset_per_scan_state works on an already-empty dict."""
    # Ensure empty
    _PER_SCAN_EVENTS.clear()
    assert len(_PER_SCAN_EVENTS) == 0

    # Reset (should not raise)
    LLMJudgmentAnalyzer._reset_per_scan_state()

    # Still empty
    assert len(_PER_SCAN_EVENTS) == 0


def test_reset_per_scan_state_allows_repopulation() -> None:
    """After reset, _PER_SCAN_EVENTS can be repopulated."""
    # Populate
    _PER_SCAN_EVENTS["scan-a"] = {"AnalyzerError"}

    # Reset
    LLMJudgmentAnalyzer._reset_per_scan_state()

    # Verify empty
    assert _PER_SCAN_EVENTS == {}

    # Repopulate
    _PER_SCAN_EVENTS["scan-b"] = {"AnalyzerUnavailable"}

    # Verify repopulation works
    assert "scan-b" in _PER_SCAN_EVENTS
    assert _PER_SCAN_EVENTS["scan-b"] == {"AnalyzerUnavailable"}
