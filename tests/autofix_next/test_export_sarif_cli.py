"""Integration tests for the ``autofix-next export-sarif`` subcommand.

Covers ACs 1, 2, 3, 4, 5, 6, 7, 14, 15, 22:

* AC 1  — the subcommand exists and is registered on the top-level CLI.
* AC 2  — flags: ``--scan-id`` (required), ``--root`` (default: cwd),
          ``--out`` (default: stdout).
* AC 3  — exit codes: 0 success, 1 missing inputs, 2 OSError / emit
          failure, 3 SARIF shape-check failure.
* AC 4  — when scan_id is not found, stderr mentions ``"not found"``
          and process exits 1.
* AC 5  — emitted SARIF has exactly one result per
          ``CandidateFindingProduced`` row under the requested scan_id.
* AC 6  — ``--out`` writes to the named path; absence streams to stdout.
* AC 7  — stdout streaming is valid SARIF JSON.
* AC 14 — missing ``events.jsonl`` -> exit 1.
* AC 15 — ``--help`` is reachable from ``python3 -m
          autofix_next.cli.main export-sarif --help``.
* AC 22 — each emitted result carries BOTH ``autofixNext/v1`` and
          ``sarif-v1`` keys under ``partialFingerprints``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from autofix_next.cli import export_sarif_command


SCAN_ID = "20260417T010203Z-aabbccdd"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write_events_jsonl(root: Path, scan_id: str = SCAN_ID, n_findings: int = 2) -> Path:
    """Write a minimal events.jsonl containing:

    1 ScanStarted row (anchor for ``scan_id``) + ``n_findings``
    CandidateFindingProduced rows.

    We wrap every row in a shallow envelope with ``event_type`` and
    ``payload`` keys so the export subcommand's envelope-aware parser
    exercises the real production path.
    """
    autofix_dir = root / ".autofix"
    autofix_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    rows.append(
        {
            "event_type": "ScanStarted",
            "payload": {
                "scan_id": scan_id,
                "commit_sha": "0" * 40,
            },
        }
    )
    for i in range(n_findings):
        rows.append(
            {
                "event_type": "CandidateFindingProduced",
                "payload": {
                    "scan_id": scan_id,
                    "finding_id": f"{i:064x}",
                    "rule_id": "unused-import.intra-file",
                    "path": f"pkg/mod_{i}.py",
                    "symbol_name": f"sym_{i}",
                    "start_line": 10 + i,
                    "end_line": 10 + i,
                },
            }
        )

    events_path = autofix_dir / "events.jsonl"
    with events_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return events_path


def _args(scan_id: str, root: Path, out: Path | None) -> argparse.Namespace:
    return argparse.Namespace(scan_id=scan_id, root=root, out=out)


# ---------------------------------------------------------------------------
# AC 5 + AC 6 + AC 22: happy path writes to --out
# ---------------------------------------------------------------------------


def test_export_sarif_writes_to_out_and_exits_zero(tmp_path: Path) -> None:
    """AC 5, 6, 22: with a valid scan_id + 2 findings, run() writes the
    SARIF file, exits 0, and each result carries both fingerprint keys."""
    _write_events_jsonl(tmp_path, scan_id=SCAN_ID, n_findings=2)
    out_path = tmp_path / "out.sarif"

    exit_code = export_sarif_command.run(_args(SCAN_ID, tmp_path, out_path))

    assert exit_code == 0
    assert out_path.is_file()

    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["version"] == "2.1.0"
    results = doc["runs"][0]["results"]
    assert len(results) == 2, f"expected 2 results, got {len(results)}: {results!r}"

    for i, result in enumerate(results):
        fps = result["partialFingerprints"]
        # AC 22: both namespace keys present.
        assert "autofixNext/v1" in fps
        assert "sarif-v1" in fps
        # AC 10/22 cross-check: autofixNext/v1 is the finding_id.
        assert fps["autofixNext/v1"] == f"{i:064x}"
        # sarif-v1 is always a 64-char lowercase hex digest.
        assert len(fps["sarif-v1"]) == 64
        assert fps["sarif-v1"] == fps["sarif-v1"].lower()


# ---------------------------------------------------------------------------
# AC 4 + AC 3: missing scan_id -> exit 1, stderr says "not found"
# ---------------------------------------------------------------------------


def test_export_sarif_missing_scan_id_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC 3, 4: requesting an unknown scan_id exits 1 and prints a
    'not found' diagnostic to stderr."""
    _write_events_jsonl(tmp_path, scan_id=SCAN_ID, n_findings=1)
    out_path = tmp_path / "shouldnt_be_written.sarif"

    exit_code = export_sarif_command.run(
        _args("20260417T999999Z-missing00", tmp_path, out_path)
    )
    assert exit_code == 1
    assert not out_path.exists(), "no SARIF file should be written on error"

    captured = capsys.readouterr()
    assert "not found" in captured.err.lower(), (
        f"expected 'not found' in stderr, got: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# AC 6 + AC 7: --out=None streams SARIF JSON to stdout
# ---------------------------------------------------------------------------


def test_export_sarif_streams_to_stdout_when_out_is_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC 6, 7: with ``--out None``, stdout receives valid SARIF JSON
    and the return code is 0."""
    _write_events_jsonl(tmp_path, scan_id=SCAN_ID, n_findings=2)

    exit_code = export_sarif_command.run(_args(SCAN_ID, tmp_path, None))
    assert exit_code == 0

    captured = capsys.readouterr()
    assert captured.out.strip(), "expected SARIF JSON on stdout, got empty"

    # stdout MUST be valid SARIF JSON.
    doc = json.loads(captured.out)
    assert doc["version"] == "2.1.0"
    assert doc["$schema"] == "https://json.schemastore.org/sarif-2.1.0.json"
    results = doc["runs"][0]["results"]
    assert len(results) == 2
    # AC 22 again: both keys present on the stdout path too.
    for result in results:
        fps = result["partialFingerprints"]
        assert "autofixNext/v1" in fps
        assert "sarif-v1" in fps


# ---------------------------------------------------------------------------
# AC 14 + AC 3: missing events.jsonl -> exit 1
# ---------------------------------------------------------------------------


def test_export_sarif_missing_events_file_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC 3, 14: without ``.autofix/events.jsonl`` the subcommand
    exits 1 and prints a diagnostic pointing at the expected path."""
    # Deliberately do NOT create .autofix/events.jsonl
    assert not (tmp_path / ".autofix" / "events.jsonl").exists()
    out_path = tmp_path / "out.sarif"

    exit_code = export_sarif_command.run(_args(SCAN_ID, tmp_path, out_path))
    assert exit_code == 1

    captured = capsys.readouterr()
    # The stderr diagnostic MUST name the file we looked for so the
    # developer can tell a missing-file case from a bad-scan-id case.
    assert "events.jsonl" in captured.err, (
        f"expected events.jsonl in stderr, got: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# AC 2: --root defaults to cwd
# ---------------------------------------------------------------------------


def test_export_sarif_root_defaults_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2: passing ``root=None`` resolves to the process cwd at run
    time — not at parser construction time."""
    _write_events_jsonl(tmp_path, scan_id=SCAN_ID, n_findings=1)
    out_path = tmp_path / "out.sarif"

    monkeypatch.chdir(tmp_path)
    # Explicit None (not the default, to prove the run-time resolution).
    exit_code = export_sarif_command.run(_args(SCAN_ID, None, out_path))
    assert exit_code == 0
    assert out_path.is_file()


# ---------------------------------------------------------------------------
# AC 1 + AC 15: subcommand registered + --help works via `python -m`
# ---------------------------------------------------------------------------


def test_export_sarif_subcommand_is_registered() -> None:
    """AC 1: building the parser exposes the ``export-sarif`` subparser
    with ``--scan-id``, ``--root``, and ``--out`` flags."""
    from autofix_next.cli.main import _build_parser

    parser = _build_parser()
    # Parse a minimal invocation to prove the dispatch exists.
    ns = parser.parse_args(["export-sarif", "--scan-id", "x"])
    assert ns.subcommand == "export-sarif"
    assert ns.scan_id == "x"
    # --out and --root default to None.
    assert ns.out is None
    assert ns.root is None
    # The runner hook is wired so the main dispatcher can call it.
    assert getattr(ns, "_runner", None) is export_sarif_command.run


def test_export_sarif_shape_check_failure_returns_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 3: emit_sarif raising TypeError/ValueError (SARIF shape-check
    failure per spec) causes export-sarif to return exit code 3."""
    events_dir = tmp_path / ".autofix"
    events_dir.mkdir()
    events_path = events_dir / "events.jsonl"
    import json as _json
    events_path.write_text(
        _json.dumps({"event_type": "ScanStarted", "payload": {"scan_id": "s1"}}) + "\n"
        + _json.dumps({
            "event_type": "CandidateFindingProduced",
            "payload": {
                "scan_id": "s1",
                "rule_id": "unused-import.intra-file",
                "path": "foo.py",
                "symbol_name": "os",
                "finding_id": "f1",
                "start_line": 1,
                "end_line": 1,
            },
        }) + "\n",
        encoding="utf-8",
    )

    def _raising_emit(scan_id, findings, sarif_path):
        raise TypeError("synthetic shape-check failure for AC 3 repair test")

    from autofix_next.cli import export_sarif_command as cmd
    monkeypatch.setattr(cmd, "emit_sarif", _raising_emit)

    # --out path (emit_sarif call at line 202)
    out_path = tmp_path / "out.sarif"
    args_out = argparse.Namespace(
        scan_id="s1", root=tmp_path, out=out_path, _runner=cmd.run,
    )
    assert cmd.run(args_out) == 3, "exit code 3 expected on shape-check failure (--out path)"

    # stdout path (emit_sarif call at line ~178 via tempfile)
    args_stdout = argparse.Namespace(
        scan_id="s1", root=tmp_path, out=None, _runner=cmd.run,
    )
    assert cmd.run(args_stdout) == 3, "exit code 3 expected on shape-check failure (stdout path)"


def test_export_sarif_help_is_reachable_via_module() -> None:
    """AC 15: ``python3 -m autofix_next.cli.main export-sarif --help``
    returns exit 0 and prints the three flags."""
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-m", "autofix_next.cli.main", "export-sarif", "--help"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"--help returned {proc.returncode}; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    combined = proc.stdout + proc.stderr
    for flag in ("--scan-id", "--root", "--out"):
        assert flag in combined, (
            f"expected {flag!r} in --help output, got: {combined!r}"
        )
