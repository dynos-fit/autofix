"""Constants for the ``start`` / ``stop`` / ``logs`` daemon commands.

Side-effect-free module. Mirrors the discipline established in
:mod:`autofix.cli.post_fix_constants`: every literal the daemon
commands depend on lives here so the command-module bodies stay
free of inline strings/numbers.
"""
from __future__ import annotations


# --- File names (under ``.autofix/``) --------------------------------------

PIDFILE_NAME: str = "crawl.pid"
"""Name of the pidfile the crawl driver writes via ``_pidfile``.

Intentionally identical to the literal ``crawl_command.driver``
already uses — the daemon commands attach to the existing
pidfile machinery rather than introducing a parallel one.
"""

DAEMON_LOG_NAME: str = "daemon.log"
"""Name of the append-only log file ``autofix start`` redirects
the daemon's stdout/stderr into. ``autofix logs`` reads from
this same file.
"""


# --- Timing knobs ----------------------------------------------------------

STARTUP_TIMEOUT_SECONDS: float = 10.0
"""How long ``autofix start`` waits for the spawned daemon to
write its pidfile before declaring the spawn a failure. The
crawl driver's ``_pidfile`` context manager writes the pidfile
synchronously on entry to ``run_crawl_continuously``, so 10s is
generous — a process that hasn't reached that line by then is
dead or hung pre-loop.
"""

STOP_TIMEOUT_SECONDS: float = 30.0
"""How long ``autofix stop`` waits after SIGTERM for the daemon
to exit before giving up. The daemon's loop iterates every
``budget.interval_seconds`` (cheap=3600, balanced=1800,
aggressive=300) and yields between cycles; SIGTERM is
delivered to the inter-cycle ``time.sleep`` and Python's
default handler raises ``KeyboardInterrupt`` immediately. 30s
covers the worst case where SIGTERM is received mid-LLM-call
(claude -p subprocess) and the daemon waits for the subprocess
to wind down.
"""


# --- ``logs`` defaults -----------------------------------------------------

DEFAULT_LOG_TAIL_LINES: int = 50
"""How many trailing lines ``autofix logs`` prints before
following. 50 is enough to catch the last cycle's
"cycle picked / aggregated / dispatcher firing / applied N"
sequence on most repos.
"""


__all__ = [
    "PIDFILE_NAME",
    "DAEMON_LOG_NAME",
    "STARTUP_TIMEOUT_SECONDS",
    "STOP_TIMEOUT_SECONDS",
    "DEFAULT_LOG_TAIL_LINES",
]
