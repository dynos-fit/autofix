"""Unit tests for ruff adapter with mocked subprocess.run.

Tests the analyze() function's handling of:
- Normal output (F401, E501)
- Missing binary (FileNotFoundError)
- Timeout (subprocess.TimeoutExpired)
- Per-scan memoization of error events
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from autofix.analyzers.linter_passthrough import ruff
from autofix.parsing.tree_sitter import ParseResult
from autofix.telemetry.correlation import set_scan_id


@pytest.fixture(autouse=True)
def reset_ruff_state():
    """Reset per-scan state before each test."""
    ruff._reset_per_scan_state()
    yield
    ruff._reset_per_scan_state()


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


class TestRuffAdapterNormalOutput:
    """Tests for successful ruff output parsing."""

    def test_f401_unused_import(self, tmp_path: Path):
        """F401 unused import produces one CandidateFinding with correct fields."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath

        ruff_output = json.dumps([{
            "code": "F401",
            "filename": str(abs_path),
            "location": {"row": 1, "column": 1},
            "end_location": {"row": 1, "column": 13},
            "message": "unused import os",
        }])

        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,  # ruff returns 1 when findings exist
                stdout=ruff_output,
                stderr="",
            )
            findings = list(ruff.analyze(parse_result, symbol_table=None))

        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == "linter:ruff:F401"
        assert f.provenance == "linter:ruff:F401"
        assert f.path == "p.py"
        assert f.start_line == 1
        assert f.end_line == 1
        assert f.symbol_name == "F401"

    def test_e501_line_too_long(self, tmp_path: Path):
        """E501 line too long produces one CandidateFinding with correct fields."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath

        ruff_output = json.dumps([{
            "code": "E501",
            "filename": str(abs_path),
            "location": {"row": 5, "column": 1},
            "end_location": {"row": 5, "column": 100},
            "message": "line too long (100 > 88 characters)",
        }])

        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,
                stdout=ruff_output,
                stderr="",
            )
            findings = list(ruff.analyze(parse_result, symbol_table=None))

        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == "linter:ruff:E501"
        assert f.provenance == "linter:ruff:E501"
        assert f.path == "p.py"
        assert f.start_line == 5
        assert f.end_line == 5
        assert f.symbol_name == "E501"

    def test_multiple_findings(self, tmp_path: Path):
        """Multiple findings in ruff output all convert to CandidateFinding."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath

        ruff_output = json.dumps([
            {
                "code": "F401",
                "filename": str(abs_path),
                "location": {"row": 1, "column": 1},
                "end_location": {"row": 1, "column": 13},
                "message": "unused import os",
            },
            {
                "code": "E501",
                "filename": str(abs_path),
                "location": {"row": 5, "column": 1},
                "end_location": {"row": 5, "column": 100},
                "message": "line too long",
            },
        ])

        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,
                stdout=ruff_output,
                stderr="",
            )
            findings = list(ruff.analyze(parse_result, symbol_table=None))

        assert len(findings) == 2
        assert findings[0].rule_id == "linter:ruff:F401"
        assert findings[1].rule_id == "linter:ruff:E501"

    def test_empty_output(self, tmp_path: Path):
        """Empty ruff output produces empty finding list."""
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
            findings = list(ruff.analyze(parse_result, symbol_table=None))

        assert findings == []


class TestRuffAdapterMissingBinary:
    """Tests for FileNotFoundError (ruff not on PATH)."""

    def test_missing_binary_returns_empty(self, tmp_path: Path):
        """FileNotFoundError returns empty iterable."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath
        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("ruff not found")
            findings = list(ruff.analyze(parse_result, symbol_table=None))

        assert findings == []

    def test_missing_binary_logs_event_once_per_scan(self, tmp_path: Path):
        """FileNotFoundError logs AnalyzerUnavailable event exactly once per scan."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath
        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("ruff not found")
            with mock.patch("autofix.telemetry.events_log.append_event") as mock_append:
                # First call
                with set_scan_id("test-scan-1"):
                    list(ruff.analyze(parse_result, symbol_table=None))
                    # Second call with same scan_id should not log again
                    list(ruff.analyze(parse_result, symbol_table=None))

        # Should have exactly one call to append_event for AnalyzerUnavailable
        calls = [c for c in mock_append.call_args_list
                 if c[0][1] == "AnalyzerUnavailable"]
        assert len(calls) == 1
        assert calls[0][0][2]["analyzer"] == "linter:ruff"


class TestRuffAdapterTimeout:
    """Tests for subprocess.TimeoutExpired."""

    def test_timeout_returns_empty(self, tmp_path: Path):
        """TimeoutExpired returns empty iterable."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath
        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["ruff", "check"],
                timeout=30,
            )
            findings = list(ruff.analyze(parse_result, symbol_table=None))

        assert findings == []

    def test_timeout_logs_event_once_per_scan(self, tmp_path: Path):
        """TimeoutExpired logs AnalyzerTimeout event exactly once per scan."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath
        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["ruff", "check"],
                timeout=30,
            )
            with mock.patch("autofix.telemetry.events_log.append_event") as mock_append:
                with set_scan_id("test-scan-2"):
                    list(ruff.analyze(parse_result, symbol_table=None))
                    # Second call with same scan_id should not log again
                    list(ruff.analyze(parse_result, symbol_table=None))

        calls = [c for c in mock_append.call_args_list
                 if c[0][1] == "AnalyzerTimeout"]
        assert len(calls) == 1
        assert calls[0][0][2]["analyzer"] == "linter:ruff"


class TestRuffAdapterPerScanMemoization:
    """Tests for per-scan error event memoization."""

    def test_same_scan_id_logs_error_once(self, tmp_path: Path):
        """Two FileNotFoundError calls with same scan_id log event only once."""
        repo_root = tmp_path
        relpath1 = "p1.py"
        relpath2 = "p2.py"
        parse_result1 = _make_parse_result(repo_root, relpath1)
        parse_result2 = _make_parse_result(repo_root, relpath2)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("ruff not found")
            with mock.patch("autofix.telemetry.events_log.append_event") as mock_append:
                with set_scan_id("test-scan-memo"):
                    # Analyze two different files in the same scan
                    list(ruff.analyze(parse_result1, symbol_table=None))
                    list(ruff.analyze(parse_result2, symbol_table=None))

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
            mock_run.side_effect = FileNotFoundError("ruff not found")
            with mock.patch("autofix.telemetry.events_log.append_event") as mock_append:
                # First scan
                with set_scan_id("scan-a"):
                    list(ruff.analyze(parse_result, symbol_table=None))
                # Second scan
                with set_scan_id("scan-b"):
                    list(ruff.analyze(parse_result, symbol_table=None))

        # Should log once per scan
        unavailable_calls = [c for c in mock_append.call_args_list
                             if c[0][1] == "AnalyzerUnavailable"]
        assert len(unavailable_calls) == 2


class TestRuffAdapterMalformedOutput:
    """Tests for subprocess output errors."""

    def test_malformed_json_returns_empty(self, tmp_path: Path):
        """Malformed JSON output returns empty iterable."""
        repo_root = tmp_path
        relpath = "p.py"
        abs_path = repo_root / relpath
        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,
                stdout="not valid json {",
                stderr="",
            )
            findings = list(ruff.analyze(parse_result, symbol_table=None))

        assert findings == []

    def test_subprocess_error_with_no_stdout(self, tmp_path: Path):
        """Subprocess error (non-zero return, no stdout) returns empty."""
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
            findings = list(ruff.analyze(parse_result, symbol_table=None))

        assert findings == []


class TestRuffAdapterPathHandling:
    """Tests for path resolution and repo root calculation."""

    def test_relpath_preserved_in_finding(self, tmp_path: Path):
        """The relative path from ParseResult is preserved in finding.path."""
        repo_root = tmp_path
        relpath = "src/module.py"
        abs_path = repo_root / relpath
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        ruff_output = json.dumps([{
            "code": "F401",
            "filename": str(abs_path),
            "location": {"row": 1, "column": 1},
            "end_location": {"row": 1, "column": 13},
            "message": "unused import",
        }])

        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,
                stdout=ruff_output,
                stderr="",
            )
            findings = list(ruff.analyze(parse_result, symbol_table=None))

        assert len(findings) == 1
        assert findings[0].path == "src/module.py"

    def test_deeply_nested_relpath(self, tmp_path: Path):
        """Deeply nested relative paths are handled correctly."""
        repo_root = tmp_path
        relpath = "a/b/c/d/e.py"
        abs_path = repo_root / relpath

        ruff_output = json.dumps([{
            "code": "F401",
            "filename": str(abs_path),
            "location": {"row": 10, "column": 5},
            "end_location": {"row": 10, "column": 20},
            "message": "unused",
        }])

        parse_result = _make_parse_result(repo_root, relpath, abs_path)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,
                stdout=ruff_output,
                stderr="",
            )
            findings = list(ruff.analyze(parse_result, symbol_table=None))

        assert len(findings) == 1
        assert findings[0].path == "a/b/c/d/e.py"
