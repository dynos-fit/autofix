"""Test malformed response handling (AC-10c).

Monkeypatched scheduler returns non-JSON, non-list, missing-key cases;
assert empty iter + AnalyzerError event each.
"""

from __future__ import annotations

import json
from unittest import mock


from autofix.telemetry.correlation import set_scan_id
from tests.autofix.analyzers.llm_judgment.conftest import FakeJudgmentAnalyzer


def test_malformed_non_json_response(parse_result) -> None:
    """Non-JSON response returns empty iterator and logs AnalyzerError event."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value="not valid json {",
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

    # Assert: AnalyzerError event logged
    error_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerError"
    ]
    assert len(error_calls) == 1
    assert error_calls[0][0][2]["analyzer"] == "llm:fake"
    assert "Failed to parse JSON" in error_calls[0][0][2]["reason"]


def test_malformed_non_list_response(parse_result) -> None:
    """Non-list JSON response returns empty iterator and logs AnalyzerError event."""
    # Return dict instead of list
    llm_response = json.dumps({"error": "unexpected format"})

    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=llm_response,
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            with set_scan_id("test-scan-2"):
                findings = list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )

    assert findings == []

    error_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerError"
    ]
    assert len(error_calls) == 1
    assert "expected list" in error_calls[0][0][2]["reason"]


def test_malformed_missing_required_keys(parse_result) -> None:
    """JSON list with missing required keys returns empty iterator and logs error."""
    # Missing 'severity' and 'evidence' keys
    llm_response = json.dumps([
        {
            "category": "test",
            "description": "Test",
            "start_line": 1,
            "end_line": 1,
        }
    ])

    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=llm_response,
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            with set_scan_id("test-scan-3"):
                findings = list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )

    assert findings == []

    error_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerError"
    ]
    assert len(error_calls) == 1
    assert "missing required keys" in error_calls[0][0][2]["reason"]


def test_malformed_non_dict_list_item(parse_result) -> None:
    """JSON list with non-dict items (e.g., string) skips them gracefully."""
    # Valid item followed by non-dict
    llm_response = json.dumps([
        {
            "category": "valid",
            "severity": "high",
            "description": "Valid item",
            "start_line": 1,
            "end_line": 1,
            "evidence": "code",
        },
        "invalid string item",
    ])

    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=llm_response,
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            findings = list(
                FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
            )

    # Should skip the invalid item and return only the valid one
    assert len(findings) == 1
    assert findings[0].rule_id == "llm:fake:valid"

    # No error should be logged (non-dict items are skipped silently)
    error_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerError"
    ]
    assert len(error_calls) == 0


def test_malformed_empty_string_response(parse_result) -> None:
    """Empty string response returns empty iterator and logs AnalyzerError event."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value="",
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            with set_scan_id("test-scan-4"):
                findings = list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )

    assert findings == []

    error_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerError"
    ]
    assert len(error_calls) == 1


def test_malformed_response_logs_raw_output(parse_result) -> None:
    """AnalyzerError event includes truncated raw response."""
    bad_response = "x" * 2000  # Long response

    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=bad_response,
    ):
        with mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            with set_scan_id("test-scan-5"):
                findings = list(
                    FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
                )

    error_calls = [
        c
        for c in mock_append.call_args_list
        if c[0][1] == "AnalyzerError"
    ]
    assert len(error_calls) == 1
    # Raw should be truncated to 1024 chars
    assert len(error_calls[0][0][2]["raw"]) == 1024
    assert error_calls[0][0][2]["raw"] == bad_response[:1024]
