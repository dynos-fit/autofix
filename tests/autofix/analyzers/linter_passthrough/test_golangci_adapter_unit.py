"""Unit tests for the golangci-lint passthrough adapter with mocked subprocess.run.

Tests the analyze() function's handling of:
- Module exports / public contract
- JSON output parsing (Issues list → CandidateFinding records)
- v2 flag form succeeds first try (memoized for the rest of the scan)
- v1 flag fallback on "unknown flag" stderr (memoized for the rest of the scan)
- Marker probe no-ops (no go.mod)
- Missing binary (FileNotFoundError) and per-scan memo behavior
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from autofix.analyzers.linter_passthrough import golangci
from autofix.parsing.tree_sitter import ParseResult
from autofix.telemetry.correlation import set_scan_id


@pytest.fixture(autouse=True)
def reset_golangci_state():
    """Reset per-scan state before and after each test."""
    golangci._reset_per_scan_state()
    yield
    golangci._reset_per_scan_state()


def _make_parse_result(
    repo_root: Path, relpath: str, abs_path: Path | None = None
) -> ParseResult:
    """Build a ParseResult instance for testing the golangci adapter."""
    if abs_path is None:
        abs_path = repo_root / relpath
    return ParseResult(
        path=abs_path,
        relpath=relpath,
        source_bytes=b"",
        tree=None,
        lines=[],
    )


def _seed_go_project(repo_root: Path) -> None:
    """Create a minimal go.mod marker at root."""
    (repo_root / "go.mod").write_text("module example.com/foo\n")


def test_exports():
    """Adapter exports the documented contract: prefix, version, analyze."""
    assert golangci.RULE_ID_PREFIX == "linter:golangci"
    assert golangci.RULE_VERSION == "v1"
    assert golangci.__all__ == ["RULE_ID_PREFIX", "RULE_VERSION", "analyze"]
    assert callable(golangci.analyze)


def test_parses_json_output_emits_finding(tmp_path: Path):
    """Valid golangci JSON with one issue emits one CandidateFinding."""
    repo_root = tmp_path
    _seed_go_project(repo_root)
    relpath = "main.go"
    abs_path = repo_root / relpath

    output = json.dumps(
        {
            "Issues": [
                {
                    "FromLinter": "errcheck",
                    "Text": "Error return value of `foo` is not checked",
                    "Pos": {
                        "Filename": str(abs_path),
                        "Line": 7,
                        "Column": 5,
                    },
                }
            ]
        }
    )

    parse_result = _make_parse_result(repo_root, relpath, abs_path)

    with mock.patch("shutil.which", return_value="/usr/local/bin/golangci-lint"):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0, stdout=output, stderr=""
            )
            findings = list(golangci.analyze(parse_result, symbol_table=None))

    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "linter:golangci:errcheck"
    assert f.provenance == "linter:golangci:errcheck"
    assert f.path == relpath
    assert f.symbol_name == "errcheck"
    assert f.start_line == 7
    assert f.end_line == 7
    assert f.changed_slice == "Error return value of `foo` is not checked"


def test_v2_flag_succeeds_first_try(tmp_path: Path):
    """First call uses --output.formats=json (v2); zero return → no fallback."""
    repo_root = tmp_path
    _seed_go_project(repo_root)
    relpath = "main.go"
    abs_path = repo_root / relpath
    parse_result = _make_parse_result(repo_root, relpath, abs_path)

    output = json.dumps({"Issues": []})

    with mock.patch("shutil.which", return_value="/usr/local/bin/golangci-lint"):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0, stdout=output, stderr=""
            )
            findings = list(golangci.analyze(parse_result, symbol_table=None))
            # Second file in the same scan also takes the v2 fast-path.
            findings += list(golangci.analyze(parse_result, symbol_table=None))

    assert findings == []
    # Exactly two subprocess calls (one per analyze) — no fallback retry.
    assert mock_run.call_count == 2

    # Every call's argv contains the v2 flag and never the v1 flag.
    for call in mock_run.call_args_list:
        argv = call.args[0]
        assert "--output.formats=json" in argv
        assert "--out-format=json" not in argv

    # Memoized as v2 for this scan_id (default "_no_scan" since no set_scan_id).
    assert golangci._PER_SCAN_FLAG_FORM["_no_scan"] == "v2"


def test_v1_flag_fallback_on_unknown_flag(tmp_path: Path):
    """v2 returns 'unknown flag' stderr → adapter retries with --out-format=json,
    memoizes v1, and a second file in the same scan goes straight to v1 (one call).
    """
    repo_root = tmp_path
    _seed_go_project(repo_root)
    relpath_a = "a.go"
    relpath_b = "b.go"
    pr_a = _make_parse_result(repo_root, relpath_a)
    pr_b = _make_parse_result(repo_root, relpath_b)

    valid_output = json.dumps(
        {
            "Issues": [
                {
                    "FromLinter": "errcheck",
                    "Text": "boom",
                    "Pos": {"Filename": str(pr_a.path), "Line": 1, "Column": 1},
                }
            ]
        }
    )

    # Sequence:
    #   call 1 (file a, v2): rc=2, stderr "unknown flag --output.formats" → fallback
    #   call 2 (file a, v1 retry): rc=0, valid stdout → finding
    #   call 3 (file b, v1 from memo): rc=0, valid stdout → finding
    responses = [
        mock.Mock(returncode=2, stdout="", stderr="unknown flag --output.formats"),
        mock.Mock(returncode=0, stdout=valid_output, stderr=""),
        mock.Mock(returncode=0, stdout=valid_output, stderr=""),
    ]

    with mock.patch("shutil.which", return_value="/usr/local/bin/golangci-lint"):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = responses
            with set_scan_id("scan-fallback"):
                findings_a = list(golangci.analyze(pr_a, symbol_table=None))
                findings_b = list(golangci.analyze(pr_b, symbol_table=None))

    # Exactly two subprocess calls on the first file (probe + retry), one on the second.
    assert mock_run.call_count == 3
    argvs = [c.args[0] for c in mock_run.call_args_list]
    assert "--output.formats=json" in argvs[0]  # initial v2 probe
    assert "--out-format=json" in argvs[1]  # v1 fallback retry
    assert "--out-format=json" in argvs[2]  # second file uses memoized v1 directly
    assert "--output.formats=json" not in argvs[2]

    assert len(findings_a) == 1
    assert findings_a[0].rule_id == "linter:golangci:errcheck"
    assert len(findings_b) == 1
    assert findings_b[0].rule_id == "linter:golangci:errcheck"

    # Memo is v1 for this scan.
    assert golangci._PER_SCAN_FLAG_FORM["scan-fallback"] == "v1"


def test_no_op_when_go_mod_missing(tmp_path: Path):
    """Without go.mod the adapter short-circuits; subprocess is never called."""
    repo_root = tmp_path  # deliberately empty
    relpath = "main.go"
    abs_path = repo_root / relpath
    parse_result = _make_parse_result(repo_root, relpath, abs_path)

    with mock.patch("subprocess.run") as mock_run, mock.patch(
        "autofix.telemetry.events_log.append_event"
    ) as mock_append:
        with set_scan_id("scan-no-gomod"):
            findings = list(golangci.analyze(parse_result, symbol_table=None))

    assert findings == []
    assert mock_run.call_count == 0
    skipped = [c for c in mock_append.call_args_list if c[0][1] == "AnalyzerSkipped"]
    assert len(skipped) == 1
    payload = skipped[0][0][2]
    assert payload["analyzer"] == "linter:golangci"
    assert payload["reason"] == "no_go_mod"


def test_no_op_when_binary_missing(tmp_path: Path):
    """shutil.which returns None → reason no_golangci_binary, no subprocess attempt."""
    repo_root = tmp_path
    _seed_go_project(repo_root)
    relpath = "main.go"
    abs_path = repo_root / relpath
    parse_result = _make_parse_result(repo_root, relpath, abs_path)

    with mock.patch("shutil.which", return_value=None) as mock_which:
        with mock.patch("subprocess.run") as mock_run, mock.patch(
            "autofix.telemetry.events_log.append_event"
        ) as mock_append:
            with set_scan_id("scan-no-bin"):
                first = list(golangci.analyze(parse_result, symbol_table=None))
                second = list(golangci.analyze(parse_result, symbol_table=None))

    assert first == []
    assert second == []
    # Subprocess never attempted because shutil.which sentinels the binary as missing.
    assert mock_run.call_count == 0
    # which is consulted at most once thanks to per-scan memoization.
    assert mock_which.call_count >= 1

    skipped = [c for c in mock_append.call_args_list if c[0][1] == "AnalyzerSkipped"]
    assert len(skipped) == 1
    assert skipped[0][0][2]["reason"] == "no_golangci_binary"


def test_per_scan_memo(tmp_path: Path):
    """Same scan_id across multiple files emits AnalyzerSkipped exactly once
    and never re-walks the filesystem or re-invokes subprocess after the
    first short-circuit decision.
    """
    repo_root = tmp_path
    # No go.mod → all calls in this scan must short-circuit on the cached marker.
    relpath_a = "a.go"
    relpath_b = "deeper/b.go"
    pr_a = _make_parse_result(repo_root, relpath_a)
    pr_b = _make_parse_result(repo_root, relpath_b)

    with mock.patch("subprocess.run") as mock_run, mock.patch(
        "autofix.telemetry.events_log.append_event"
    ) as mock_append:
        with set_scan_id("scan-shared"):
            list(golangci.analyze(pr_a, symbol_table=None))
            list(golangci.analyze(pr_b, symbol_table=None))
            list(golangci.analyze(pr_a, symbol_table=None))

    assert mock_run.call_count == 0
    skipped = [c for c in mock_append.call_args_list if c[0][1] == "AnalyzerSkipped"]
    # Exactly one event for the entire scan despite three analyze() calls.
    assert len(skipped) == 1
    assert skipped[0][0][2]["reason"] == "no_go_mod"
