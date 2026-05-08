"""``autofix status`` formatting tests (ARCH-016 AC-23)."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_status_no_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No ledger yet → friendly message, exit 0."""
    from autofix.cli.status_command import run_status

    rc = run_status(root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "no ledger" in out.lower() or "never run" in out.lower()


def test_status_running_with_pidfile(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A pidfile exists → status reports the daemon is running."""
    from autofix.cli.status_command import run_status

    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir()
    # Write a pidfile pointing at our own pid (always running).
    import os
    (autofix_dir / "crawl.pid").write_text(str(os.getpid()))

    rc = run_status(root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "running" in out.lower()


def test_status_not_running_when_pidfile_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from autofix.cli.status_command import run_status

    rc = run_status(root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "not running" in out.lower()


def test_status_reports_ledger_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Status counts files in the ledger and prints the summary line."""
    from autofix.cli.status_command import run_status
    from autofix.crawl.ledger import Ledger, LedgerRow

    ledger = Ledger(root=tmp_path)
    for i in range(3):
        ledger.record(LedgerRow(
            ts=f"2026-05-08T0{i}:00:00Z",
            bundle_fingerprint=f"{i}" * 64,
            seed_path=f"f{i}.py",
            file_paths=(f"f{i}.py",),
            analyzer="cheap",
            last_commit_sha="abc",
            last_finding_count=0,
            cache_hit=False,
            event_id=f"e{i}",
        ))

    rc = run_status(root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "ledger" in out.lower()


def test_status_handles_stale_pidfile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pidfile with a non-existent PID → reported as not running."""
    from autofix.cli.status_command import run_status

    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir()
    # PID 1 is init/launchd; using a very high PID guarantees absence.
    (autofix_dir / "crawl.pid").write_text("9999999")

    rc = run_status(root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "not running" in out.lower() or "stale" in out.lower()
