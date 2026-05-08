"""``autofix stop`` — clean shutdown of the running crawl daemon.

Sends SIGTERM to the PID stored in ``.autofix/crawl.pid`` and waits
up to ``_STOP_TIMEOUT_SECONDS`` for the process to exit cleanly. The
daemon's ``_pidfile`` context manager removes the pidfile in its
``finally`` block on either clean exit or unhandled exception, so
the pidfile disappearing is our exit signal.

Two failure modes the command surfaces explicitly to the operator:

* No pidfile (and no autofix process discoverable via ``ps``) →
  exit 1 with "not running".
* Pidfile exists, SIGTERM sent, but the process is still alive after
  the timeout → exit 2 with "daemon did not exit; try `kill -9
  <pid>` if needed". We deliberately do NOT escalate to SIGKILL
  ourselves — a daemon that ignores SIGTERM is a bug worth
  investigating, not papering over.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from autofix.cli.daemon_constants import (
    PIDFILE_NAME,
    STOP_TIMEOUT_SECONDS,
)


def run_stop(*, root: Path) -> int:
    """Stop the running crawl daemon.

    Returns:
        0 on clean shutdown.
        1 if no daemon was running for this repo.
        2 if SIGTERM was delivered but the daemon did not exit
          within the timeout.
    """
    pidfile = Path(root) / ".autofix" / PIDFILE_NAME
    pid = _read_pid(pidfile)
    if pid is None or not _process_alive(pid):
        # Stale or missing pidfile — clean it up best-effort and tell
        # the operator there was nothing to stop.
        if pidfile.exists():
            try:
                pidfile.unlink()
            except OSError:
                pass
        print("autofix: not running.")
        return 1

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Race: the process died between the alive-check and the
        # signal. Treat as "already stopped".
        print(f"autofix: not running (PID {pid} already exited).")
        return 0
    except PermissionError:
        print(
            f"autofix: cannot signal PID {pid} (permission denied). "
            "Was it started by a different user?",
            file=sys.stderr,
        )
        return 2

    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            print(f"autofix: stopped (PID {pid}).")
            return 0
        time.sleep(0.2)

    print(
        f"autofix: PID {pid} did not exit within {STOP_TIMEOUT_SECONDS}s. "
        f"Try `kill -9 {pid}` to force.",
        file=sys.stderr,
    )
    return 2


def _read_pid(pidfile: Path) -> int | None:
    try:
        return int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


__all__ = ["run_stop"]
