"""Test seam unavailable scenario (AC-10d).

Monkeypatched Scheduler.invoke_judgment raises AnalyzerSeamUnavailableError;
assert empty iter + AnalyzerUnavailable event.
"""

from __future__ import annotations

from unittest import mock


from autofix.llm.scheduler import AnalyzerSeamUnavailableError
from autofix.telemetry.correlation import set_scan_id
from tests.autofix.analyzers.llm_judgment.conftest import FakeJudgmentAnalyzer


def test_seam_unavailable_returns_empty_and_logs_event(parse_result) -> None:
    """AnalyzerSeamUnavailableError returns empty iterator and logs event."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        side_effect=AnalyzerSeamUnavailableError("Scheduler unavailable"),
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            with set_scan_id("test-scan-1"):
                findings = list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )

    # Assert: empty iterator
    assert findings == []

    # Assert: AnalyzerUnavailable event logged
    unavailable_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerUnavailable"
    ]
    assert len(unavailable_calls) == 1
    assert unavailable_calls[0][0][2]["analyzer"] == "llm:fake"


def test_seam_unavailable_logs_once_per_scan(parse_result) -> None:
    """AnalyzerSeamUnavailableError logs event only once per scan_id."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        side_effect=AnalyzerSeamUnavailableError("Scheduler unavailable"),
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            with set_scan_id("test-scan-2"):
                # First call
                list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )
                # Second call with same scan_id
                list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )

    # Should log exactly once for the entire scan
    unavailable_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerUnavailable"
    ]
    assert len(unavailable_calls) == 1


def test_seam_unavailable_different_scans_log_independently(parse_result) -> None:
    """Different scan_ids each log their own AnalyzerUnavailable event."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        side_effect=AnalyzerSeamUnavailableError("Scheduler unavailable"),
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            with set_scan_id("scan-a"):
                list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )
            with set_scan_id("scan-b"):
                list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )

    unavailable_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerUnavailable"
    ]
    assert len(unavailable_calls) == 2


def test_seam_unavailable_no_cache_written(parse_result) -> None:
    """AnalyzerSeamUnavailableError prevents cache from being written."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        side_effect=AnalyzerSeamUnavailableError("Scheduler unavailable"),
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ):
            findings = list(
                FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
            )

    # Verify no cache file was written
    cache_dir = parse_result.repo_root / ".autofix" / "cache" / "llm_judgment"
    if cache_dir.exists():
        cache_files = list(cache_dir.glob("*.json"))
        assert len(cache_files) == 0
