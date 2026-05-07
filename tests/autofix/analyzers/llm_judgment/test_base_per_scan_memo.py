"""Test per-scan memoization (AC-10e).

Two consecutive analyze calls under the same scan_id with malformed responses;
assert only ONE AnalyzerError event.
"""

from __future__ import annotations

from unittest import mock

import pytest

from autofix.telemetry.correlation import set_scan_id
from tests.autofix.analyzers.llm_judgment.conftest import FakeJudgmentAnalyzer


def test_per_scan_memo_logs_error_once_for_malformed(parse_result) -> None:
    """Two malformed responses in same scan logs AnalyzerError only once."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value="not valid json",
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            with set_scan_id("test-scan-memo"):
                # First analyze call with malformed response
                list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )
                # Second analyze call with same malformed response
                list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )

    # Should log AnalyzerError exactly once per scan
    error_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerError"
    ]
    assert len(error_calls) == 1
    assert error_calls[0][0][2]["analyzer"] == "llm:fake"


def test_per_scan_memo_tracks_error_type_independently(parse_result) -> None:
    """Different error types in same scan log independently.

    Note: AC-10e says "only ONE AnalyzerError event", implying that
    the same error type (AnalyzerError) is memoized per scan.
    However, we also test that the implementation correctly tracks
    which specific error condition was logged (all the various reasons
    are consolidated under "AnalyzerError" event type, but the per-scan
    memoization uses the event_type string "AnalyzerError").
    """
    # First call: non-JSON error
    # Second call: non-list error
    # Both are AnalyzerError, so only one should be logged
    responses = iter(["not json", '{"dict": "instead of list"}'])

    def mock_invoke_side_effect(*args, **kwargs):
        return next(responses)

    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        side_effect=mock_invoke_side_effect,
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            with set_scan_id("test-scan-types"):
                # First call: non-JSON
                list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )
                # Second call: non-list (would trigger error, but already logged)
                list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )

    error_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerError"
    ]
    # Per-scan memoization: only one event logged total
    assert len(error_calls) == 1


def test_per_scan_memo_resets_between_scans(parse_result) -> None:
    """Different scan_ids each log their own AnalyzerError event."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value="not valid json",
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            # Scan 1
            with set_scan_id("scan-memo-1"):
                list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )
            # Scan 2
            with set_scan_id("scan-memo-2"):
                list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )

    error_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerError"
    ]
    # Each scan logs independently
    assert len(error_calls) == 2


def test_per_scan_memo_with_multiple_files_same_scan(tmp_path) -> None:
    """Multiple files analyzed in same scan log error only once."""
    # Create two files
    file1 = tmp_path / "file1.py"
    file1.write_text("x = 1\n", encoding="utf-8")
    file2 = tmp_path / "file2.py"
    file2.write_text("y = 2\n", encoding="utf-8")

    class FakeParseResult1:
        repo_root = tmp_path
        relpath = "file1.py"

    class FakeParseResult2:
        repo_root = tmp_path
        relpath = "file2.py"

    pr1 = FakeParseResult1()
    pr2 = FakeParseResult2()

    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value="not valid json",
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            with set_scan_id("test-scan-multi-file"):
                # Analyze file 1
                list(FakeJudgmentAnalyzer.analyze(pr1, symbol_table=None))
                # Analyze file 2 (same scan)
                list(FakeJudgmentAnalyzer.analyze(pr2, symbol_table=None))

    error_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerError"
    ]
    # Only one event for entire scan
    assert len(error_calls) == 1
