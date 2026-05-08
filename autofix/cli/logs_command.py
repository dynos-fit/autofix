"""``autofix logs`` — follow ``.autofix/daemon.log``.

Streams the daemon log to stdout. Three modes:

* ``autofix logs`` — equivalent to ``tail -n 50 -f``: show the last
  50 lines, then keep streaming new output until interrupted.
* ``autofix logs --lines N`` / ``-n N`` — show the last N lines and
  exit (no follow).
* ``autofix logs --follow`` — explicit form of the default; useful
  when scripting around it.

Uses the system ``tail`` binary rather than re-implementing the
follow loop in Python — ``tail -F`` handles log rotation, deletion,
and re-creation correctly across macOS and Linux, and the operator's
existing tail customizations (color, ``less +F``, etc.) compose
naturally on top.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from autofix.cli.daemon_constants import (
    DAEMON_LOG_NAME,
    DEFAULT_LOG_TAIL_LINES,
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Wire ``-n`` / ``--lines`` / ``--follow`` onto the parser."""
    parser.add_argument(
        "-n", "--lines",
        type=int,
        default=DEFAULT_LOG_TAIL_LINES,
        help=(
            f"Number of trailing lines to print "
            f"(default: {DEFAULT_LOG_TAIL_LINES})."
        ),
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        default=None,
        help=(
            "Stream new log lines as they arrive. Default is to "
            "follow when no other flag is set; pass --no-follow to "
            "show the tail and exit."
        ),
    )
    parser.add_argument(
        "--no-follow",
        dest="follow",
        action="store_false",
        help="Show the tail and exit without streaming.",
    )


def run_logs(
    *, root: Path, lines: int, follow: bool | None,
) -> int:
    """Stream the daemon log.

    Returns:
        0 on clean exit (Ctrl-C or end-of-file).
        1 if the log file does not exist (daemon never started).
        2 if ``tail`` is not installed.
    """
    log_path = Path(root) / ".autofix" / DAEMON_LOG_NAME
    if not log_path.exists():
        print(
            f"autofix: no daemon log at {log_path}. "
            "Run `autofix start` first.",
            file=sys.stderr,
        )
        return 1

    tail_bin = shutil.which("tail")
    if tail_bin is None:
        print(
            "autofix: `tail` not found in PATH. "
            "Read the file directly: " + str(log_path),
            file=sys.stderr,
        )
        return 2

    # Default behavior: follow. Operators run `autofix logs` and
    # expect a live stream — same shape as ``docker logs -f``,
    # ``journalctl -fu``, etc.
    if follow is None:
        follow = True

    args = [tail_bin, "-n", str(max(lines, 0))]
    if follow:
        # ``-F`` (capital) follows by name + retries on rotation,
        # which matches the daemon's append-only-but-rotatable log
        # discipline. ``-f`` (lowercase) follows by descriptor and
        # silently stops streaming if the file is rotated.
        args.append("-F")
    args.append(str(log_path))

    try:
        # Inherit stdout/stderr — tail streams directly to the
        # operator's terminal. Catch KeyboardInterrupt so Ctrl-C
        # during ``-F`` exits cleanly with code 0 rather than the
        # 130 the shell would otherwise see.
        return subprocess.call(args)  # noqa: S603
    except KeyboardInterrupt:
        return 0


__all__ = ["add_arguments", "run_logs"]
