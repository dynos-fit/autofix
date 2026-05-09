"""Pidfile lifecycle for ``--once`` mode + status fallback (regression).

Bugs fixed:

1. ``run_crawl_once`` didn't manage the pidfile — only
   ``run_crawl_continuously`` did. So
   ``autofix --root . --once`` would run for minutes (an
   aggressive cycle = 100 LLM calls) but ``autofix status``
   reported "not running" the whole time. The pidfile lifecycle
   is now lifted into a shared context manager used by BOTH
   entry points.

2. ``status`` only checked the pidfile. If the pidfile got
   deleted (manual rm, stale cleanup, daemon crash mid-cycle),
   status would lie even though a daemon was clearly running.
   Now status falls back to scanning the process table for any
   ``autofix --root ...`` invocation.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _git_init(tmp_path: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("# a\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)


def test_run_crawl_once_writes_pidfile(tmp_path: Path) -> None:
    """During ``run_crawl_once``, the pidfile exists with our PID."""
    from autofix.cli import cycle_runner as driver

    _git_init(tmp_path)
    pidfile = tmp_path / ".autofix" / "crawl.pid"
    captured: dict = {}

    def _capture_pid_during_cycle(**_kwargs):
        captured["pidfile_exists_during_cycle"] = pidfile.exists()
        if pidfile.exists():
            captured["pid_during_cycle"] = int(pidfile.read_text().strip())
        return 0

    with patch(
        "autofix.cli.cycle_runner._run_crawl_once_body",
        side_effect=_capture_pid_during_cycle,
    ):
        rc = driver.run_crawl_once(
            root=tmp_path, mode="preview", budget="cheap", quiet=True,
        )

    assert rc == 0
    assert captured["pidfile_exists_during_cycle"] is True
    assert captured["pid_during_cycle"] == os.getpid()


def test_run_crawl_once_removes_pidfile_on_clean_exit(tmp_path: Path) -> None:
    """After ``run_crawl_once`` exits cleanly, the pidfile is gone."""
    from autofix.cli import cycle_runner as driver

    _git_init(tmp_path)
    pidfile = tmp_path / ".autofix" / "crawl.pid"

    with patch("autofix.cli.cycle_runner._run_crawl_once_body", return_value=0):
        driver.run_crawl_once(
            root=tmp_path, mode="preview", budget="cheap", quiet=True,
        )
    assert not pidfile.exists()


def test_run_crawl_once_removes_pidfile_on_exception(tmp_path: Path) -> None:
    """Exception inside the cycle still cleans up the pidfile."""
    from autofix.cli import cycle_runner as driver

    _git_init(tmp_path)
    pidfile = tmp_path / ".autofix" / "crawl.pid"

    with patch(
        "autofix.cli.cycle_runner._run_crawl_once_body",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError):
            driver.run_crawl_once(
                root=tmp_path, mode="preview", budget="cheap", quiet=True,
            )
    assert not pidfile.exists()


def test_continuous_loop_still_writes_pidfile(tmp_path: Path) -> None:
    """``run_crawl_continuously`` keeps its pidfile semantics intact."""
    from autofix.cli import cycle_runner as driver

    _git_init(tmp_path)
    pidfile = tmp_path / ".autofix" / "crawl.pid"
    captured: dict = {"saw_pidfile": False}

    def _check_then_quit(**_kwargs):
        captured["saw_pidfile"] = pidfile.exists()
        raise KeyboardInterrupt

    with patch(
        "autofix.cli.cycle_runner._run_crawl_once_body",
        side_effect=_check_then_quit,
    ), patch("autofix.cli.cycle_runner._sleep", lambda _: None):
        rc = driver.run_crawl_continuously(
            root=tmp_path, mode="preview", budget="cheap",
            interval_seconds=1, quiet=True,
        )
    assert rc == 0
    assert captured["saw_pidfile"] is True
    assert not pidfile.exists()  # cleaned up on KeyboardInterrupt


# --- status: pgrep-style fallback when pidfile is missing ----------------


def test_status_fallback_finds_running_daemon(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No pidfile but a running ``autofix --root`` process → status
    reports the PID via the ps fallback."""
    from autofix.cli.status_command import run_status

    fake_ps = (
        "  PID ARGS\n"
        f"  101 {os.getpid()}\n"  # filtered (own pid)
        "  202 python autofix --root /repo\n"  # match
        "  303 nginx\n"
    )

    fake_proc = MagicMock(stdout=fake_ps)

    with patch("subprocess.run", return_value=fake_proc):
        rc = run_status(root=tmp_path)

    out = capsys.readouterr().out
    assert rc == 0
    assert "PID 202" in out
    assert "pidfile missing" in out


def test_status_fallback_returns_not_running_when_no_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No pidfile and no autofix process in ps → status says not running."""
    from autofix.cli.status_command import run_status

    fake_ps = "  PID ARGS\n  101 nginx\n  202 vim\n"
    fake_proc = MagicMock(stdout=fake_ps)

    with patch("subprocess.run", return_value=fake_proc):
        rc = run_status(root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "not running" in out


def test_status_fallback_filters_out_status_invocation_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``autofix status --root .`` is itself ``autofix --root`` in ps —
    must not be counted as a running daemon."""
    from autofix.cli.status_command import run_status

    fake_ps = (
        "  PID ARGS\n"
        "  202 python autofix status --root /repo\n"
        "  203 python autofix --root /repo status\n"
    )
    fake_proc = MagicMock(stdout=fake_ps)

    with patch("subprocess.run", return_value=fake_proc):
        rc = run_status(root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "not running" in out


def test_status_pidfile_takes_precedence_over_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the pidfile exists, ps fallback is not consulted."""
    from autofix.cli.status_command import run_status

    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir()
    (autofix_dir / "crawl.pid").write_text(str(os.getpid()))

    sentinel = MagicMock(side_effect=AssertionError("ps must not be called"))
    with patch("subprocess.run", sentinel):
        rc = run_status(root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert f"PID {os.getpid()}" in out
    sentinel.assert_not_called()
