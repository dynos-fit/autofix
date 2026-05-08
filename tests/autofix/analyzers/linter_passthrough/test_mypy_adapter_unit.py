"""Unit tests for mypy adapter with mocked subprocess.run.

Tests the analyze() function's handling of:
- Normal output (assignment error, unknown error)
- Note and warning lines (skipped)
- Garbage stdout
- Missing binary (FileNotFoundError)
- Timeout (subprocess.TimeoutExpired)
- Per-scan memoization of error events
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from autofix.analyzers.linter_passthrough import mypy
from autofix.parsing.tree_sitter import ParseResult
from autofix.telemetry.correlation import set_scan_id


@pytest.fixture(autouse=True)
def reset_mypy_state():
    """Reset per-scan state before each test."""
    mypy._reset_per_scan_state()
    yield
    mypy._reset_per_scan_state()


def _make_parse_result(
    repo_root: Path, relpath: str, abs_path: Path | None = None
) -> ParseResult:
    """Build a fake ParseResult for testing."""
    if abs_path is None:
        abs_path = repo_root / relpath
    return ParseResult(
        path=abs_path,
        relpath=relpath,
        source_bytes=b"",
        tree=None,
        lines=[],
    )


class TestMypyAdapterNormalOutput:
    """Tests for successful mypy output parsing."""

    def test_assignment_error_emits_finding(self, tmp_path: Path):
        """Assignment error produces one CandidateFinding with correct fields."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath

        mypy_output = f'{abs_path}:5:1: error: Incompatible types in assignment  [assignment]\n'

        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,
                stdout=mypy_output,
                stderr="",
            )
            findings = list(mypy.analyze(parse_result, symbol_table=None))

        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == "linter:mypy:assignment"
        assert f.provenance == "linter:mypy:assignment"
        assert f.path == "p.py"
        assert f.start_line == 5
        assert f.end_line == 5
        assert f.symbol_name == "assignment"

    def test_error_without_code_falls_back_to_unknown(self, tmp_path: Path):
        """Error without code falls back to 'unknown' code."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath

        mypy_output = f'{abs_path}:7: error: Some message without code\n'

        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,
                stdout=mypy_output,
                stderr="",
            )
            findings = list(mypy.analyze(parse_result, symbol_table=None))

        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == "linter:mypy:unknown"
        assert f.symbol_name == "unknown"

    def test_note_line_skipped(self, tmp_path: Path):
        """Note lines are skipped and produce no findings."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath

        mypy_output = f'{abs_path}:5:1: note: Some note message\n'

        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout=mypy_output,
                stderr="",
            )
            findings = list(mypy.analyze(parse_result, symbol_table=None))

        assert findings == []

    def test_warning_line_skipped(self, tmp_path: Path):
        """Warning lines are skipped and produce no findings."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath

        mypy_output = f'{abs_path}:5:1: warning: Some warning message\n'

        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout=mypy_output,
                stderr="",
            )
            findings = list(mypy.analyze(parse_result, symbol_table=None))

        assert findings == []

    def test_garbage_stdout_no_findings(self, tmp_path: Path):
        """Garbage stdout that doesn't match the expected format produces no findings."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath

        mypy_output = "random non-matching text\n"

        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,
                stdout=mypy_output,
                stderr="",
            )
            findings = list(mypy.analyze(parse_result, symbol_table=None))

        assert findings == []

    def test_multiple_errors(self, tmp_path: Path):
        """Multiple error lines produce multiple findings."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath

        mypy_output = (
            f'{abs_path}:5:1: error: Incompatible types in assignment  [assignment]\n'
            f'{abs_path}:10:5: error: Name "undefined" is not defined  [name-defined]\n'
        )

        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,
                stdout=mypy_output,
                stderr="",
            )
            findings = list(mypy.analyze(parse_result, symbol_table=None))

        assert len(findings) == 2
        assert findings[0].rule_id == "linter:mypy:assignment"
        assert findings[0].start_line == 5
        assert findings[1].rule_id == "linter:mypy:name-defined"
        assert findings[1].start_line == 10

    def test_empty_output(self, tmp_path: Path):
        """Empty mypy output produces empty finding list."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath

        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout="",
                stderr="",
            )
            findings = list(mypy.analyze(parse_result, symbol_table=None))

        assert findings == []


class TestMypyAdapterMissingBinary:
    """Tests for FileNotFoundError (mypy not on PATH)."""

    def test_missing_binary_returns_empty(self, tmp_path: Path):
        """FileNotFoundError returns empty iterable."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath
        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("mypy not found")
            findings = list(mypy.analyze(parse_result, symbol_table=None))

        assert findings == []

    def test_missing_binary_logs_event_once_per_scan(self, tmp_path: Path):
        """FileNotFoundError logs AnalyzerUnavailable event exactly once per scan."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath
        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("mypy not found")
            with mock.patch("autofix.telemetry.events_log.append_event") as mock_append:
                # First call
                with set_scan_id("test-scan-1"):
                    list(mypy.analyze(parse_result, symbol_table=None))
                    # Second call with same scan_id should not log again
                    list(mypy.analyze(parse_result, symbol_table=None))

        # Should have exactly one call to append_event for AnalyzerUnavailable
        calls = [c for c in mock_append.call_args_list
                 if c[0][1] == "AnalyzerUnavailable"]
        assert len(calls) == 1
        assert calls[0][0][2]["analyzer"] == "linter:mypy"


class TestMypyAdapterTimeout:
    """Tests for subprocess.TimeoutExpired."""

    def test_timeout_returns_empty(self, tmp_path: Path):
        """TimeoutExpired returns empty iterable."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath
        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["mypy"],
                timeout=60,
            )
            findings = list(mypy.analyze(parse_result, symbol_table=None))

        assert findings == []

    def test_timeout_logs_event_once_per_scan(self, tmp_path: Path):
        """TimeoutExpired logs AnalyzerTimeout event exactly once per scan."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath
        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["mypy"],
                timeout=60,
            )
            with mock.patch("autofix.telemetry.events_log.append_event") as mock_append:
                with set_scan_id("test-scan-2"):
                    list(mypy.analyze(parse_result, symbol_table=None))
                    # Second call with same scan_id should not log again
                    list(mypy.analyze(parse_result, symbol_table=None))

        calls = [c for c in mock_append.call_args_list
                 if c[0][1] == "AnalyzerTimeout"]
        assert len(calls) == 1
        assert calls[0][0][2]["analyzer"] == "linter:mypy"
        # AC-6 payload contract: the file path MUST be in the
        # AnalyzerTimeout event payload.
        assert calls[0][0][2]["file"] == relpath
        assert calls[0][0][2]["scan_id"] == "test-scan-2"

    def test_timeout_logs_event_per_file_per_scan(self, tmp_path: Path):
        """AC-6: dedupe key is (scan_id, file). Timeouts on DIFFERENT
        files in the same scan MUST each emit telemetry — only repeats
        on the SAME file are suppressed."""
        repo_root = tmp_path
        parse_a = _make_parse_result(repo_root, "a.py", repo_root / "a.py")
        parse_b = _make_parse_result(repo_root, "b.py", repo_root / "b.py")

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["mypy"], timeout=60,
            )
            with mock.patch("autofix.telemetry.events_log.append_event") as mock_append:
                with set_scan_id("test-scan-perfile"):
                    list(mypy.analyze(parse_a, symbol_table=None))
                    list(mypy.analyze(parse_b, symbol_table=None))
                    # Repeat on a.py — must NOT log a third time.
                    list(mypy.analyze(parse_a, symbol_table=None))

        timeout_calls = [c for c in mock_append.call_args_list
                         if c[0][1] == "AnalyzerTimeout"]
        # Two unique files → two telemetry events; the repeat on a.py
        # is suppressed by the per-(scan_id, file) memo.
        assert len(timeout_calls) == 2
        files_seen = {c[0][2]["file"] for c in timeout_calls}
        assert files_seen == {"a.py", "b.py"}


class TestMypyAdapterPerScanMemoization:
    """Tests for per-scan error event memoization."""

    def test_per_scan_memo_emits_warning_once(self, tmp_path: Path):
        """Two FileNotFoundError calls with same scan_id emit event only once."""
        repo_root = tmp_path
        relpath1 = "p1.py"
        relpath2 = "p2.py"
        parse_result1 = _make_parse_result(repo_root, relpath1)
        parse_result2 = _make_parse_result(repo_root, relpath2)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("mypy not found")
            # Create a tmp_path/.autofix/events.jsonl for events logging
            events_dir = tmp_path / ".autofix"
            events_dir.mkdir(parents=True, exist_ok=True)
            events_file = events_dir / "events.jsonl"

            with set_scan_id("test-1"):
                # Analyze two different files in the same scan
                list(mypy.analyze(parse_result1, symbol_table=None))
                list(mypy.analyze(parse_result2, symbol_table=None))

            # Count AnalyzerUnavailable events in events.jsonl
            if events_file.exists():
                import json
                events = [
                    json.loads(line)
                    for line in events_file.read_text().splitlines()
                    if line
                ]
                unavailable_events = [
                    e for e in events
                    if e.get("event") == "AnalyzerUnavailable"
                ]
                assert len(unavailable_events) == 1

    def test_same_scan_id_logs_error_once(self, tmp_path: Path):
        """Two FileNotFoundError calls with same scan_id log event only once."""
        repo_root = tmp_path
        relpath1 = "p1.py"
        relpath2 = "p2.py"
        parse_result1 = _make_parse_result(repo_root, relpath1)
        parse_result2 = _make_parse_result(repo_root, relpath2)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("mypy not found")
            with mock.patch("autofix.telemetry.events_log.append_event") as mock_append:
                with set_scan_id("test-scan-memo"):
                    # Analyze two different files in the same scan
                    list(mypy.analyze(parse_result1, symbol_table=None))
                    list(mypy.analyze(parse_result2, symbol_table=None))

        # Should log only once for the entire scan
        unavailable_calls = [c for c in mock_append.call_args_list
                             if c[0][1] == "AnalyzerUnavailable"]
        assert len(unavailable_calls) == 1

    def test_different_scan_ids_log_error_independently(self, tmp_path: Path):
        """Different scan_ids log error events independently."""
        repo_root = tmp_path
        relpath = "p.py"
        parse_result = _make_parse_result(repo_root, relpath)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("mypy not found")
            with mock.patch("autofix.telemetry.events_log.append_event") as mock_append:
                # First scan
                with set_scan_id("scan-a"):
                    list(mypy.analyze(parse_result, symbol_table=None))
                # Second scan
                with set_scan_id("scan-b"):
                    list(mypy.analyze(parse_result, symbol_table=None))

        # Should log once per scan
        unavailable_calls = [c for c in mock_append.call_args_list
                             if c[0][1] == "AnalyzerUnavailable"]
        assert len(unavailable_calls) == 2


class TestMypyAdapterMalformedOutput:
    """Tests for subprocess output errors."""

    def test_subprocess_error_with_no_stdout(self, tmp_path: Path):
        """Subprocess error (non-zero return, no stdout) returns empty AND
        emits AnalyzerError telemetry per AC-7."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath
        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=127,
                stdout="",
                stderr="command not found",
            )
            with mock.patch("autofix.telemetry.events_log.append_event") as mock_append:
                with set_scan_id("test-scan-err"):
                    findings = list(mypy.analyze(parse_result, symbol_table=None))

        assert findings == []
        # AC-7: AnalyzerError telemetry MUST fire on the
        # subprocess-error-with-no-stdout path.
        err_calls = [c for c in mock_append.call_args_list
                     if c[0][1] == "AnalyzerError"]
        assert len(err_calls) == 1
        payload = err_calls[0][0][2]
        assert payload["analyzer"] == "linter:mypy"
        assert payload["scan_id"] == "test-scan-err"
        assert "command not found" in payload.get("stderr", "")
