"""``autofix start`` / ``stop`` / ``logs`` end-to-end behavior.

The trio replaces the manual
``nohup autofix --root . > .autofix/daemon.log 2>&1 &`` +
``kill "$(cat .autofix/crawl.pid)"`` + ``tail -f .autofix/daemon.log``
incantations with a single integrated UX. These tests pin:

* ``start`` writes the pidfile (via the spawned daemon's
  ``_pidfile`` context manager) and refuses to launch a second
  daemon when one is already alive.
* ``stop`` SIGTERMs the running daemon and reports clean exit; a
  call with no daemon present reports "not running" and cleans up
  any stale pidfile.
* ``logs`` requires the log file to exist; fails with a clear
  message when the daemon was never started.

The "spawned daemon" in these tests is a tiny throwaway Python
process — we do NOT exercise the real crawl loop (that needs a
git repo, ledger, LLM, etc). The contract being tested is the
process lifecycle + pidfile handshake, not the crawl semantics.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from autofix.cli import (
    logs_command,
    start_command,
    stop_command,
)
from autofix.cli.daemon_constants import (
    DAEMON_LOG_NAME,
    PIDFILE_NAME,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spawn_fake_daemon(root: Path) -> subprocess.Popen:
    """Spawn a tiny Python process that mimics the daemon: writes
    the pidfile and sleeps until SIGTERM.
    """
    autofix_dir = root / ".autofix"
    autofix_dir.mkdir(parents=True, exist_ok=True)
    pidfile = autofix_dir / PIDFILE_NAME

    code = (
        "import os, sys, time, signal\n"
        f"pidfile = {str(pidfile)!r}\n"
        "with open(pidfile, 'w') as f:\n"
        "    f.write(str(os.getpid()))\n"
        "def _term(signum, frame):\n"
        "    try: os.unlink(pidfile)\n"
        "    except OSError: pass\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, _term)\n"
        "while True:\n"
        "    time.sleep(0.1)\n"
    )
    proc = subprocess.Popen(  # noqa: S603 — controlled arg list
        [sys.executable, "-c", code],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for the pidfile to land before returning so the test
    # doesn't race against the child's startup.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if pidfile.exists():
            return proc
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("fake daemon did not write pidfile")


def _cleanup_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def test_stop_sigterms_pid_and_polls_until_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM is sent to the pidfile's PID, and the polling loop
    exits as soon as ``_process_alive`` reports the process is gone.

    Mocked rather than spawning a real subprocess: in pytest the
    test process is the spawned daemon's parent and won't reap
    zombies, so ``os.kill(pid, 0)`` keeps reporting "alive" forever.
    Production isn't affected — ``autofix start`` returns
    immediately, the daemon gets re-parented to init, and init
    reaps zombies. The integration is exercised by
    ``test_start_then_stop_round_trip`` below.
    """
    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir()
    pidfile = autofix_dir / PIDFILE_NAME
    pidfile.write_text("12345")

    sigterm_calls: list[tuple[int, int]] = []
    alive_calls = {"count": 0}

    def _fake_kill(pid: int, sig: int) -> None:
        sigterm_calls.append((pid, sig))

    def _fake_alive(pid: int) -> bool:
        # First two probes: still alive. Third: dead.
        alive_calls["count"] += 1
        return alive_calls["count"] < 3

    monkeypatch.setattr(stop_command.os, "kill", _fake_kill)
    monkeypatch.setattr(stop_command, "_process_alive", _fake_alive)

    rc = stop_command.run_stop(root=tmp_path)

    assert rc == 0
    assert sigterm_calls == [(12345, signal.SIGTERM)]
    # Polled until the third probe reported "not alive".
    assert alive_calls["count"] == 3


def test_stop_when_not_running_returns_1_and_cleans_stale_pidfile(
    tmp_path: Path,
) -> None:
    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir()
    pidfile = autofix_dir / PIDFILE_NAME
    # Stale: a PID that's almost certainly dead.
    pidfile.write_text("999999")

    rc = stop_command.run_stop(root=tmp_path)

    assert rc == 1
    assert not pidfile.exists(), "stale pidfile must be cleaned up"


def test_stop_with_no_pidfile_at_all_returns_1(tmp_path: Path) -> None:
    rc = stop_command.run_stop(root=tmp_path)
    assert rc == 1


def test_stop_reports_failure_if_daemon_ignores_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon that doesn't honor SIGTERM hits the timeout branch
    and we tell the operator to escalate manually rather than
    SIGKILL silently.
    """
    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir()
    pidfile = autofix_dir / PIDFILE_NAME
    pidfile.write_text(str(os.getpid()))  # ourselves — definitely alive

    monkeypatch.setattr(stop_command, "STOP_TIMEOUT_SECONDS", 0.5)
    # No-op the kill so the process never actually dies.
    monkeypatch.setattr(stop_command.os, "kill", lambda pid, sig: None)

    rc = stop_command.run_stop(root=tmp_path)
    assert rc == 2


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_start_refuses_when_daemon_already_running(tmp_path: Path) -> None:
    proc = _spawn_fake_daemon(tmp_path)
    try:
        rc = start_command.run_start(root=tmp_path)
    finally:
        _cleanup_proc(proc)

    assert rc == 1


def test_start_returns_2_when_spawned_process_never_writes_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the spawned binary never reaches ``_pidfile`` we must NOT
    silently report success. The operator should be pointed at the
    log to find out why.
    """
    monkeypatch.setattr(start_command, "STARTUP_TIMEOUT_SECONDS", 0.5)
    # Spawn ``true`` (or a Python equivalent) which exits immediately
    # without writing a pidfile.
    monkeypatch.setattr(
        start_command.shutil, "which",
        lambda _: sys.executable,
    )
    # Replace the autofix invocation with `python -c "pass"`
    # which exits 0 immediately.
    real_popen = subprocess.Popen

    def _fake_popen(args, **kwargs):
        return real_popen(
            [sys.executable, "-c", "pass"],
            **{k: v for k, v in kwargs.items() if k != "cwd"},
        )

    monkeypatch.setattr(start_command.subprocess, "Popen", _fake_popen)

    rc = start_command.run_start(root=tmp_path)
    assert rc == 2


def test_start_actually_launches_and_records_pidfile(tmp_path: Path) -> None:
    """Integration: ``start`` spawns a detached child process that
    reaches the pidfile-writing code path. We swap the autofix
    binary for a throwaway script that mimics what the crawl
    driver's ``_pidfile`` context manager does.

    We deliberately do NOT also exercise ``stop`` here: pytest is
    the spawned process's parent and does not reap zombies, so
    ``os.kill(pid, 0)`` keeps reporting "alive" after SIGTERM and
    the polling loop in ``stop`` would block until its 30s
    timeout. ``stop``'s polling logic is covered unit-style by
    ``test_stop_sigterms_pid_and_polls_until_exit``.
    """
    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir(parents=True, exist_ok=True)
    pidfile = autofix_dir / PIDFILE_NAME

    fake_daemon_script = tmp_path / "fake_daemon.py"
    fake_daemon_script.write_text(
        "import os, sys, time\n"
        f"pidfile = {str(pidfile)!r}\n"
        "with open(pidfile, 'w') as f:\n"
        "    f.write(str(os.getpid()))\n"
        "while True:\n"
        "    time.sleep(0.1)\n"
    )

    fake_argv = [sys.executable, str(fake_daemon_script)]
    real_popen = subprocess.Popen
    spawned: list[subprocess.Popen] = []

    def _fake_popen(args, **kwargs):
        proc = real_popen(fake_argv, **kwargs)
        spawned.append(proc)
        return proc

    try:
        with patch.object(
            start_command.subprocess, "Popen", side_effect=_fake_popen
        ):
            rc = start_command.run_start(root=tmp_path)

        assert rc == 0, "start should succeed when daemon writes pidfile"
        assert pidfile.exists()

        recorded_pid = int(pidfile.read_text())
        assert spawned, "Popen was never invoked"
        assert recorded_pid == spawned[0].pid
    finally:
        for proc in spawned:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                pass


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


def test_logs_returns_1_when_daemon_log_missing(tmp_path: Path) -> None:
    rc = logs_command.run_logs(root=tmp_path, lines=10, follow=False)
    assert rc == 1


def test_logs_prints_tail_when_log_exists(
    tmp_path: Path, capfd: pytest.CaptureFixture[str],
) -> None:
    """``logs --no-follow -n 3`` prints the last 3 lines and exits."""
    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir()
    log_path = autofix_dir / DAEMON_LOG_NAME
    log_path.write_text("\n".join(f"line {i}" for i in range(1, 6)) + "\n")

    rc = logs_command.run_logs(root=tmp_path, lines=3, follow=False)

    out, _ = capfd.readouterr()
    assert rc == 0
    # Last 3 lines.
    assert "line 3" in out
    assert "line 4" in out
    assert "line 5" in out
    assert "line 1" not in out


def test_logs_default_is_follow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare ``autofix logs`` (no flags) defaults to follow=True."""
    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir()
    (autofix_dir / DAEMON_LOG_NAME).write_text("seed\n")

    captured: list[str] = []

    def _fake_call(args):
        captured.extend(args)
        return 0

    monkeypatch.setattr(logs_command.subprocess, "call", _fake_call)

    # follow=None means "default", which the run_logs body resolves
    # to True.
    rc = logs_command.run_logs(root=tmp_path, lines=10, follow=None)
    assert rc == 0
    assert "-F" in captured, (
        f"expected `tail -F` (follow); got argv: {captured!r}"
    )


def test_logs_no_follow_omits_F_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir()
    (autofix_dir / DAEMON_LOG_NAME).write_text("seed\n")

    captured: list[str] = []

    def _fake_call(args):
        captured.extend(args)
        return 0

    monkeypatch.setattr(logs_command.subprocess, "call", _fake_call)

    rc = logs_command.run_logs(root=tmp_path, lines=10, follow=False)
    assert rc == 0
    assert "-F" not in captured


# ---------------------------------------------------------------------------
# main.py wiring
# ---------------------------------------------------------------------------


def test_main_dispatches_start(tmp_path: Path) -> None:
    """`autofix start --root <p>` routes into start_command.run_start."""
    from autofix.cli import main as main_mod

    captured = {}

    def _fake_start(*, root):
        captured["root"] = root
        return 0

    with patch.object(start_command, "run_start", side_effect=_fake_start):
        rc = main_mod.main(["autofix", "start", "--root", str(tmp_path)])

    assert rc == 0
    assert captured["root"] == tmp_path


def test_main_dispatches_stop(tmp_path: Path) -> None:
    from autofix.cli import main as main_mod

    captured = {}

    def _fake_stop(*, root):
        captured["root"] = root
        return 0

    with patch.object(stop_command, "run_stop", side_effect=_fake_stop):
        rc = main_mod.main(["autofix", "stop", "--root", str(tmp_path)])

    assert rc == 0
    assert captured["root"] == tmp_path


def test_main_dispatches_logs(tmp_path: Path) -> None:
    from autofix.cli import main as main_mod

    captured = {}

    def _fake_logs(*, root, lines, follow):
        captured["root"] = root
        captured["lines"] = lines
        captured["follow"] = follow
        return 0

    with patch.object(logs_command, "run_logs", side_effect=_fake_logs):
        rc = main_mod.main([
            "autofix", "logs", "--root", str(tmp_path), "-n", "100",
        ])

    assert rc == 0
    assert captured["root"] == tmp_path
    assert captured["lines"] == 100


def test_quickstart_help_lists_new_commands(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The bare ``autofix`` help must list start / stop / logs so
    operators discover them without reading the source.
    """
    from autofix.cli import main as main_mod

    rc = main_mod.main(["autofix"])
    out, _ = capfd.readouterr()

    assert rc == 0
    assert "autofix start" in out
    assert "autofix stop" in out
    assert "autofix logs" in out
