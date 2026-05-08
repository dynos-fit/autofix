"""``autofix start`` — daemonize the continuous crawl.

Replaces the manual ``nohup autofix --root . > .autofix/daemon.log 2>&1 &``
incantation with a single command that:

1. Refuses to launch if a daemon is already running for this repo.
2. Spawns ``autofix --root <root>`` via ``subprocess.Popen`` with
   ``start_new_session=True`` so the child outlives the launching
   shell (cleaner than a manual ``nohup`` + redirect).
3. Redirects child stdout/stderr to ``.autofix/daemon.log``
   (append, never truncate — operator can keep history across
   start/stop cycles).
4. Polls ``.autofix/crawl.pid`` for up to ``_STARTUP_TIMEOUT_SECONDS``
   seconds to confirm the daemon entered the crawl loop. If the
   pidfile never appears, the spawn is treated as a failure and the
   operator is told to inspect the log.

The daemon process is the same ``autofix --root <p>`` continuous-crawl
binary the operator could invoke manually — there is no separate
"daemon mode" path. ``status`` / ``stop`` / ``logs`` interact with
this child through the existing pidfile + log file.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from autofix.cli.daemon_constants import (
    DAEMON_LOG_NAME,
    PIDFILE_NAME,
    STARTUP_TIMEOUT_SECONDS,
)


def run_start(*, root: Path) -> int:
    """Start the continuous crawl as a detached background process.

    Returns:
        0 on successful spawn (pidfile observed within timeout).
        1 if a daemon is already running for this repo.
        2 if the spawn was issued but the pidfile never appeared
          (likely an immediate crash — operator should read the log).
    """
    autofix_dir = Path(root) / ".autofix"
    pidfile = autofix_dir / PIDFILE_NAME
    log_path = autofix_dir / DAEMON_LOG_NAME

    if _daemon_alive(pidfile):
        existing = _read_pid(pidfile)
        print(
            f"autofix: already running (PID {existing}); "
            f"`autofix stop` to halt it, `autofix status` to inspect.",
            file=sys.stderr,
        )
        return 1

    autofix_dir.mkdir(parents=True, exist_ok=True)

    # Resolve the autofix binary the same way the user invoked us;
    # falling back to ``shutil.which`` for robustness if argv[0] was
    # an absolute path that is no longer in $PATH (e.g. a venv that
    # was deactivated).
    autofix_bin = shutil.which("autofix") or sys.argv[0] or "autofix"

    # ``start_new_session=True`` puts the child in its own session
    # (= its own process group + no controlling terminal). This is
    # the modern Python idiom for "detach from the launching shell"
    # — equivalent to ``nohup`` + ``setsid`` without the shell
    # gymnastics.  Append to the log file so successive starts
    # accumulate history rather than truncate it.
    log_fd = log_path.open("a", encoding="utf-8")
    try:
        log_fd.write(f"\n=== autofix start: {_now()} ===\n")
        log_fd.flush()
        subprocess.Popen(  # noqa: S603 — argv is autofix's own binary
            [autofix_bin, "--root", str(root)],
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(root),
            start_new_session=True,
            close_fds=True,
        )
    finally:
        # The Popen child holds its own dup'd fd; we can close ours
        # so the parent process doesn't keep the file open.
        log_fd.close()

    # Wait for the daemon to write its pidfile. The crawl driver
    # writes the pidfile inside ``_pidfile`` before entering the
    # cycle loop, so a missing pidfile after the timeout means the
    # child died before reaching that code path (import error, bad
    # config, missing dependency, etc.). Operator should read the
    # log to find out which.
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if pidfile.exists():
            pid = _read_pid(pidfile)
            if pid is not None and _process_alive(pid):
                print(
                    f"autofix: started (PID {pid}); "
                    f"logs at {log_path}, `autofix logs` to follow."
                )
                return 0
        time.sleep(0.2)

    print(
        f"autofix: spawned but never reached the crawl loop "
        f"(no pidfile after {STARTUP_TIMEOUT_SECONDS}s). "
        f"Inspect {log_path} for the failure.",
        file=sys.stderr,
    )
    return 2


def _daemon_alive(pidfile: Path) -> bool:
    """True iff the pidfile exists AND the named PID is still running."""
    pid = _read_pid(pidfile)
    if pid is None:
        return False
    return _process_alive(pid)


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


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["run_start"]
