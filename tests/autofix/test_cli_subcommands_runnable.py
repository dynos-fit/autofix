"""Smoke tests for all 5 autofix subcommands invoked via main([...]).

Covers AC 20-22 (spec §20, §21, §22):

- AC 20: five smoke tests covering scan, watch, replay, export-sarif,
         policy --show; each invokes autofix.cli.main.main([...])
         directly and asserts exit 0.
- AC 21: shared module-scoped fixture builds a minimal git-initialized repo
         in tmp_path containing .git/, one trivial Python file, a
         .autofix/autofix-policy.json, and a seeded .autofix/events.jsonl
         with one ScanStarted row for scan_id="smoke-test-001".
- AC 22: unittest.mock.patch stubs run_scan, replay, emit_sarif so heavy
         pipeline internals are not exercised.

Cross-check C-06: the ScanStarted row in events.jsonl is verified against
autofix/events/schema.py ScanEvent fields before fixture authoring.
ScanEvent fields: event_type, repo_id, commit_sha, base_sha,
watcher_confidence, source, scan_id, extra. The events.jsonl row uses a
flat shape where scan_id and event_type are top-level (the replay engine
probes both envelope shapes as shown in replay.py lines 173-185).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module-scoped fixture — shared across all 5 smoke tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def reference_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a minimal git-initialized repo for all 5 smoke tests.

    Contents:
    - .git/ (via git init)
    - sample.py with content 'def main(): return 42'
    - .autofix/autofix-policy.json with {"llm_tiered": false}
    - .autofix/events.jsonl with one ScanStarted row for scan_id="smoke-test-001"
      (cross-checked against ScanEvent fields per concern C-06)

    The fixture is idempotent across test methods: each test reads from
    it but never mutates the policy file or events.jsonl.
    """
    repo = tmp_path_factory.mktemp("ref-repo")

    # Initialize a real git repo
    subprocess.run(
        ["git", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    # One trivial Python file
    (repo / "sample.py").write_text("def main(): return 42\n", encoding="utf-8")

    # .autofix directory with policy + events
    autofix_dir = repo / ".autofix"
    autofix_dir.mkdir()

    (autofix_dir / "autofix-policy.json").write_text(
        '{"llm_tiered": false}\n', encoding="utf-8"
    )

    # Cross-check C-06: ScanEvent dataclass fields from schema.py are:
    #   event_type, repo_id, commit_sha, base_sha, watcher_confidence,
    #   source, scan_id, extra
    # The replay engine (replay.py lines 173-185) accepts rows in two
    # shapes: {scan_event: {...}} or {payload: {...}} or flat dict.
    # We use a flat row where scan_id and event_type are at the top level
    # (the "else: payload = row" branch in replay.py line 179).
    scan_started_row: dict[str, Any] = {
        "event_type": "ScanStarted",
        "scan_id": "smoke-test-001",
        "repo_id": "ref-repo",
        "commit_sha": "0" * 40,
        "base_sha": None,
        "watcher_confidence": "diff-head1",
        "source": "cli",
        "extra": {
            "changeset_paths": ["sample.py"],
            "policy_sha256": "",
            "analyzer_version": "v1",
            "tree_sitter_version": "unknown",
            "changeset_watcher_confidence": "diff-head1",
        },
    }
    (autofix_dir / "events.jsonl").write_text(
        json.dumps(scan_started_row) + "\n", encoding="utf-8"
    )

    # Commit everything so git history exists
    subprocess.run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=T", "add", "."],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c", "user.email=t@t.com",
            "-c", "user.name=T",
            "commit",
            "-m", "init",
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    return repo


# ---------------------------------------------------------------------------
# AC 20: scan smoke test
# ---------------------------------------------------------------------------


def test_scan_runnable(
    reference_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 20: autofix scan exits 0 with run_scan stubbed out.

    Uses monkeypatch.chdir to ensure --root resolves to the fixture repo.
    """
    from autofix.funnel.pipeline import ScanResult

    stub_result = ScanResult(
        scan_id="smoke-test-scan",
        findings=[],
        schedule_decisions=[],
    )

    # Stub run_scan so the heavy pipeline is bypassed
    with patch(
        "autofix.funnel.pipeline.run_scan",
        return_value=stub_result,
    ):
        # Also stub emit_sarif to avoid writing SARIF to disk
        with patch("autofix.telemetry.sarif.emit_sarif"):
            monkeypatch.chdir(reference_repo)

            from autofix.cli.main import main

            exit_code = main(["scan", "--root", str(reference_repo)])

    assert exit_code == 0, f"expected exit 0 for 'scan', got {exit_code}"


# ---------------------------------------------------------------------------
# AC 20: watch smoke test
# ---------------------------------------------------------------------------


def test_watch_runnable(
    reference_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 20: autofix watch exits 0 with AUTOFIX_WATCH_ONCE=1
    and WatcherSession stubbed out.

    Uses pytest.importorskip to skip cleanly on hosts without pywatchman.
    The WatcherSession is always patched so the real Watchman daemon is
    never contacted.
    """
    pytest.importorskip("pywatchman")

    monkeypatch.setenv("AUTOFIX_WATCH_ONCE", "1")

    fake_session = MagicMock()
    fake_session.next_files.return_value = (False, ["sample.py"])

    from autofix.funnel.pipeline import ScanResult

    stub_result = ScanResult(
        scan_id="smoke-test-watch",
        findings=[],
        schedule_decisions=[],
    )

    with patch(
        "autofix.cli.watch_command.WatcherSession",
        return_value=fake_session,
    ):
        with patch(
            "autofix.funnel.pipeline.run_scan",
            return_value=stub_result,
        ):
            from autofix.cli.main import main

            exit_code = main(["watch", "--root", str(reference_repo)])

    assert exit_code == 0, f"expected exit 0 for 'watch', got {exit_code}"


# ---------------------------------------------------------------------------
# AC 20: replay smoke test
# ---------------------------------------------------------------------------


def test_replay_runnable(
    reference_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 20: autofix replay exits 0 with replay() stubbed to return
    a 'match' ScanRun so version-drift does not affect the test."""
    from autofix.telemetry.replay import ScanRun

    stub_run = ScanRun(
        scan_id="smoke-test-001",
        commit_sha="0" * 40,
        verdict="match",
        historical_findings=(),
        reproduced_findings=(),
        matched=(),
        historical_only=(),
        reproduced_only=(),
        prompt_prefix_hash_matched=(),
        prompt_prefix_hash_mismatched=(),
        drift_reason=None,
    )

    with patch(
        "autofix.cli.replay_command.replay",
        return_value=stub_run,
    ):
        from autofix.cli.main import main

        exit_code = main(
            [
                "replay",
                "--scan-id", "smoke-test-001",
                "--root", str(reference_repo),
            ]
        )

    assert exit_code == 0, f"expected exit 0 for 'replay', got {exit_code}"


# ---------------------------------------------------------------------------
# AC 20: export-sarif smoke test
# ---------------------------------------------------------------------------


def test_export_sarif_runnable(
    reference_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC 20: autofix export-sarif exits 0 with emit_sarif stubbed and
    a seeded events.jsonl containing the ScanStarted anchor."""
    out_path = tmp_path / "smoke.sarif"

    with patch(
        "autofix.cli.export_sarif_command.emit_sarif"
    ) as mock_emit:
        mock_emit.return_value = None
        # Write the SARIF stub on the expected path so the command doesn't
        # fail trying to read it back for stdout-streaming
        def _write_stub(scan_id, findings, sarif_path):
            sarif_path.parent.mkdir(parents=True, exist_ok=True)
            sarif_path.write_text(
                '{"version":"2.1.0","$schema":"https://json.schemastore.org/sarif-2.1.0.json","runs":[]}',
                encoding="utf-8",
            )

        mock_emit.side_effect = _write_stub

        from autofix.cli.main import main

        exit_code = main(
            [
                "export-sarif",
                "--scan-id", "smoke-test-001",
                "--root", str(reference_repo),
                "--out", str(out_path),
            ]
        )

    assert exit_code == 0, f"expected exit 0 for 'export-sarif', got {exit_code}"


# ---------------------------------------------------------------------------
# AC 20: policy --show smoke test
# ---------------------------------------------------------------------------


def test_policy_show_runnable(
    reference_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 20: autofix policy --show exits 0 and stdout contains 'llm_tiered'."""
    from autofix.cli.main import main

    exit_code = main(
        [
            "policy",
            "--show",
            "--root", str(reference_repo),
        ]
    )

    assert exit_code == 0, f"expected exit 0 for 'policy --show', got {exit_code}"

    captured = capsys.readouterr()
    assert "llm_tiered" in captured.out, (
        f"expected 'llm_tiered' in stdout, got: {captured.out!r}"
    )
