"""Unit tests for the eslint passthrough adapter with mocked subprocess.run.

Tests the analyze() function's handling of:
- Module exports / public contract
- JSON output parsing (severity==2 only)
- Severity filter (drops warnings + off)
- Null ruleId fallback to "unknown"
- Marker probe no-ops (no package.json / no eslint config)
- Missing binary (FileNotFoundError) and per-scan memo behavior
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from autofix.analyzers.linter_passthrough import eslint
from autofix.parsing.tree_sitter import ParseResult
from autofix.telemetry.correlation import set_scan_id


@pytest.fixture(autouse=True)
def reset_eslint_state():
    """Reset per-scan state before and after each test."""
    eslint._reset_per_scan_state()
    yield
    eslint._reset_per_scan_state()


def _make_parse_result(
    repo_root: Path, relpath: str, abs_path: Path | None = None
) -> ParseResult:
    """Build a ParseResult instance for testing the eslint adapter."""
    if abs_path is None:
        abs_path = repo_root / relpath
    return ParseResult(
        path=abs_path,
        relpath=relpath,
        source_bytes=b"",
        tree=None,
        lines=[],
    )


def _seed_eslint_project(repo_root: Path, *, with_config: bool = True) -> None:
    """Create a minimal package.json (and optional eslint config) at root."""
    (repo_root / "package.json").write_text("{}")
    if with_config:
        (repo_root / ".eslintrc.json").write_text("{}")


def test_exports():
    """Adapter exports the documented contract: prefix, version, analyze."""
    assert eslint.RULE_ID_PREFIX == "linter:eslint"
    assert eslint.RULE_VERSION == "v1"
    assert eslint.__all__ == ["RULE_ID_PREFIX", "RULE_VERSION", "analyze"]
    assert callable(eslint.analyze)


def test_parses_json_output_emits_finding(tmp_path: Path):
    """Valid eslint JSON with one severity==2 message emits one CandidateFinding."""
    repo_root = tmp_path
    _seed_eslint_project(repo_root)
    relpath = "src/app.js"
    abs_path = repo_root / relpath
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text("var x = 1;\n")

    eslint_output = json.dumps(
        [
            {
                "filePath": str(abs_path),
                "messages": [
                    {
                        "ruleId": "no-unused-vars",
                        "severity": 2,
                        "message": "x is defined but never used",
                        "line": 1,
                        "column": 5,
                        "endLine": 1,
                        "endColumn": 6,
                    },
                ],
            }
        ]
    )

    parse_result = _make_parse_result(repo_root, relpath, abs_path)

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=1,  # eslint exits 1 when problems exist
            stdout=eslint_output,
            stderr="",
        )
        findings = list(eslint.analyze(parse_result, symbol_table=None))

    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "linter:eslint:no-unused-vars"
    assert f.provenance == "linter:eslint:no-unused-vars"
    assert f.path == relpath
    assert f.symbol_name == "no-unused-vars"
    assert f.start_line == 1
    assert f.end_line == 1
    assert f.changed_slice == "x is defined but never used"


def test_severity_filter_drops_warnings(tmp_path: Path):
    """severity==1 (warning) and severity==0 (off) drop; only severity==2 emits."""
    repo_root = tmp_path
    _seed_eslint_project(repo_root)
    relpath = "app.js"
    abs_path = repo_root / relpath

    # First call: only a severity==1 message → no findings.
    output_warn_only = json.dumps(
        [
            {
                "filePath": str(abs_path),
                "messages": [
                    {
                        "ruleId": "semi",
                        "severity": 1,
                        "message": "Missing semicolon.",
                        "line": 3,
                        "column": 10,
                    },
                    {
                        "ruleId": "off-rule",
                        "severity": 0,
                        "message": "off",
                        "line": 4,
                        "column": 1,
                    },
                ],
            }
        ]
    )
    parse_result = _make_parse_result(repo_root, relpath, abs_path)
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=1, stdout=output_warn_only, stderr=""
        )
        findings = list(eslint.analyze(parse_result, symbol_table=None))
    assert findings == []

    # Second call: a severity==2 message → exactly one finding.
    output_error = json.dumps(
        [
            {
                "filePath": str(abs_path),
                "messages": [
                    {
                        "ruleId": "no-undef",
                        "severity": 2,
                        "message": "'foo' is not defined.",
                        "line": 7,
                        "column": 3,
                    }
                ],
            }
        ]
    )
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=1, stdout=output_error, stderr=""
        )
        findings = list(eslint.analyze(parse_result, symbol_table=None))
    assert len(findings) == 1
    assert findings[0].rule_id == "linter:eslint:no-undef"


def test_null_rule_id_uses_sentinel(tmp_path: Path):
    """A message with ruleId: null produces a finding tagged ':unknown'."""
    repo_root = tmp_path
    _seed_eslint_project(repo_root)
    relpath = "broken.js"
    abs_path = repo_root / relpath

    eslint_output = json.dumps(
        [
            {
                "filePath": str(abs_path),
                "messages": [
                    {
                        "ruleId": None,
                        "severity": 2,
                        "message": "Parsing error: Unexpected token",
                        "line": 1,
                        "column": 1,
                    }
                ],
            }
        ]
    )

    parse_result = _make_parse_result(repo_root, relpath, abs_path)

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=1, stdout=eslint_output, stderr=""
        )
        findings = list(eslint.analyze(parse_result, symbol_table=None))

    assert len(findings) == 1
    f = findings[0]
    # The implementation maps null/missing ruleId to "unknown".
    assert f.rule_id == "linter:eslint:unknown"
    assert f.rule_id.endswith("unknown")
    assert f.symbol_name == "unknown"


def test_no_op_when_package_json_missing(tmp_path: Path):
    """Without package.json the adapter short-circuits and never calls subprocess."""
    repo_root = tmp_path  # deliberately empty
    relpath = "app.js"
    abs_path = repo_root / relpath
    parse_result = _make_parse_result(repo_root, relpath, abs_path)

    with mock.patch("subprocess.run") as mock_run, mock.patch(
        "autofix.telemetry.events_log.append_event"
    ) as mock_append:
        with set_scan_id("scan-no-pkg"):
            findings = list(eslint.analyze(parse_result, symbol_table=None))

    assert findings == []
    assert mock_run.call_count == 0
    skipped = [c for c in mock_append.call_args_list if c[0][1] == "AnalyzerSkipped"]
    assert len(skipped) == 1
    payload = skipped[0][0][2]
    assert payload["analyzer"] == "linter:eslint"
    assert payload["reason"] == "no_package_json"


def test_no_op_when_eslint_config_missing(tmp_path: Path):
    """package.json present but no eslint config → reason no_eslint_config."""
    repo_root = tmp_path
    _seed_eslint_project(repo_root, with_config=False)
    relpath = "app.js"
    abs_path = repo_root / relpath
    parse_result = _make_parse_result(repo_root, relpath, abs_path)

    with mock.patch("subprocess.run") as mock_run, mock.patch(
        "autofix.telemetry.events_log.append_event"
    ) as mock_append:
        with set_scan_id("scan-no-cfg"):
            findings = list(eslint.analyze(parse_result, symbol_table=None))

    assert findings == []
    assert mock_run.call_count == 0
    skipped = [c for c in mock_append.call_args_list if c[0][1] == "AnalyzerSkipped"]
    assert len(skipped) == 1
    assert skipped[0][0][2]["reason"] == "no_eslint_config"


def test_no_op_when_binary_missing(tmp_path: Path):
    """FileNotFoundError → reason no_eslint_binary, then short-circuit on retry."""
    repo_root = tmp_path
    _seed_eslint_project(repo_root)
    relpath = "app.js"
    abs_path = repo_root / relpath
    parse_result = _make_parse_result(repo_root, relpath, abs_path)

    with mock.patch("subprocess.run") as mock_run, mock.patch(
        "autofix.telemetry.events_log.append_event"
    ) as mock_append:
        mock_run.side_effect = FileNotFoundError("eslint not on PATH")
        with set_scan_id("scan-no-bin"):
            first = list(eslint.analyze(parse_result, symbol_table=None))
            second = list(eslint.analyze(parse_result, symbol_table=None))

    assert first == []
    assert second == []

    # Subprocess attempted exactly once; the second call short-circuits on the
    # memoized binary=False marker.
    assert mock_run.call_count == 1

    skipped = [c for c in mock_append.call_args_list if c[0][1] == "AnalyzerSkipped"]
    assert len(skipped) == 1
    assert skipped[0][0][2]["reason"] == "no_eslint_binary"


def test_per_scan_memo_skips_repeat_subprocess(tmp_path: Path):
    """Multiple analyze() calls with same scan_id → subprocess called once when binary missing."""
    repo_root = tmp_path
    _seed_eslint_project(repo_root)
    relpath_a = "a.js"
    relpath_b = "b.js"
    pr_a = _make_parse_result(repo_root, relpath_a)
    pr_b = _make_parse_result(repo_root, relpath_b)

    with mock.patch("subprocess.run") as mock_run, mock.patch(
        "autofix.telemetry.events_log.append_event"
    ) as mock_append:
        mock_run.side_effect = FileNotFoundError("eslint not on PATH")
        with set_scan_id("scan-memo"):
            list(eslint.analyze(pr_a, symbol_table=None))
            list(eslint.analyze(pr_b, symbol_table=None))
            list(eslint.analyze(pr_a, symbol_table=None))

    # Subprocess invoked exactly once across three analyze() calls in same scan.
    assert mock_run.call_count == 1
    skipped = [c for c in mock_append.call_args_list if c[0][1] == "AnalyzerSkipped"]
    assert len(skipped) == 1
    assert skipped[0][0][2]["reason"] == "no_eslint_binary"
