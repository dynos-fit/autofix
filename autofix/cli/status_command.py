"""``autofix status`` — peek at the running daemon + ledger (ARCH-016 AC-23)."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from autofix.crawl.crawl_constants import LEDGER_FILENAME
from autofix.crawl.ledger import Ledger


_PIDFILE_NAME = "crawl.pid"


def run_status(*, root: Path) -> int:
    """Read the ledger + pidfile and print a 4-line summary."""
    autofix_dir = Path(root) / ".autofix"
    pidfile = autofix_dir / _PIDFILE_NAME
    ledger_log = autofix_dir / LEDGER_FILENAME

    # Line 1: running status.
    print(_running_line(pidfile))

    # Line 2 + 3: ledger stats.
    if not ledger_log.exists():
        print("ledger:  no ledger yet — autofix has never run on this repo")
        print("recent:  n/a")
        print("next:    n/a")
        return 0

    ledger = Ledger(root=root)
    ledger.replay_from_disk()
    rows = list(ledger)

    files_seen: set[str] = set()
    findings_24h = 0
    most_recent = None
    cutoff = _utcnow().replace(hour=0)  # rough 24h cutoff
    for r in rows:
        files_seen.update(r.file_paths)
        try:
            ts = _parse(r.ts)
        except ValueError:
            continue
        if ts >= cutoff:
            findings_24h += r.last_finding_count
        if most_recent is None or ts > most_recent[0]:
            most_recent = (ts, r)

    print(
        f"ledger:  {len(files_seen)} files seen, "
        f"{findings_24h} findings recorded in last 24h"
    )
    if most_recent is not None:
        ts, r = most_recent
        delta = _utcnow() - ts
        mins = int(delta.total_seconds() // 60)
        print(
            f"recent:  {r.bundle_fingerprint[:8]} "
            f"({mins}m ago) — {r.analyzer} on {r.seed_path}"
        )
    else:
        print("recent:  n/a")

    if pidfile.exists():
        print("next:    cycle pending (driver running)")
    else:
        print("next:    n/a (run `autofix` to start the crawl)")
    return 0


def _running_line(pidfile: Path) -> str:
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
        except (OSError, ValueError):
            return "running: not running (stale pidfile, unreadable)"
        if _process_alive(pid):
            return f"running: PID {pid}"
        return f"running: not running (stale pidfile, PID {pid})"

    # No pidfile — fall back to scanning the process table for a
    # python process whose argv contains "autofix" + "--root". This
    # catches the case where a daemon is running but the pidfile was
    # deleted (stale cleanup, manual rm, etc.). Best-effort: any
    # subprocess error means "we couldn't tell" → assume not running.
    found = _find_autofix_process()
    if found is not None:
        return f"running: PID {found} (pidfile missing — process found via ps)"
    return "running: not running"


def _find_autofix_process() -> int | None:
    """Find a running ``autofix`` daemon by scanning the process
    table. Returns the PID of the most-recently-started match, or
    None.

    Matches a process whose argv contains both ``autofix`` and
    ``--root`` — covers ``autofix --root <p>`` (continuous loop)
    and ``autofix --root <p> --once`` (single-cycle).
    """
    import subprocess

    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid,args"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None

    own_pid = os.getpid()
    candidates: list[int] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == own_pid:
            continue
        argv = parts[1]
        if "autofix" not in argv:
            continue
        if "--root" not in argv:
            continue
        # Filter out the status command itself (which contains
        # "autofix" in argv but is NOT the daemon).
        if " status" in argv or argv.endswith("status"):
            continue
        candidates.append(pid)
    if not candidates:
        return None
    return candidates[0]


def _process_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` raises if the process is gone or denied."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — still alive.
        return True
    return True


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


__all__ = ["run_status"]
