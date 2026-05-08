"""Top-level entry for the ``autofix`` console script.

Bare ``autofix`` is the **default** operator-facing command: when
called with ``--root <path>`` (and no explicit subcommand), it
dispatches to the continuous-crawl driver. Layered help: bare
``autofix --help`` prints the dumb-user 6-line summary;
``autofix --help-advanced`` lists every subcommand and flag.

The existing subcommands (``scan``, ``fix``, ``run``, ``replay``,
``export-sarif``, ``watch``, ``policy``) keep working with no
behavioral change. The new top-level subcommands ``init`` and
``status`` ship alongside.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from autofix.cli import (
    export_sarif_command,
    fix_command,
    init_command,
    logs_command,
    policy_command,
    replay_command,
    run_command,
    scan_command,
    start_command,
    status_command,
    stop_command,
    watch_command,
)


# Subcommand names argparse owns. Used to route between the bare-
# crawl path and the argparse path. ``init`` and ``status`` are
# handled OUTSIDE argparse (they have minimal flags + interactive
# prompts; argparse's subparser model adds no value).
_ARGPARSE_SUBCOMMANDS: tuple[str, ...] = (
    "scan",
    "fix",
    "run",
    "replay",
    "export-sarif",
    "watch",
    "policy",
)
_TOP_LEVEL_SUBCOMMANDS: tuple[str, ...] = (
    "init",
    "status",
    "start",
    "stop",
    "logs",
) + _ARGPARSE_SUBCOMMANDS


_DUMB_USER_HELP = """\
autofix — find and fix bugs in your code

  autofix init         Set up autofix for this repo (one-time)
  autofix start        Run autofix in the background, forever
  autofix status       Show what autofix is doing right now
  autofix logs         Tail the daemon log
  autofix stop         Stop the background daemon
  autofix --once       Run one cycle in the foreground, then exit

For one-shot or advanced commands: autofix --help-advanced
For docs: https://github.com/dynos-fit/autofix
"""


def _build_parser() -> argparse.ArgumentParser:
    """Assemble the top-level argparse parser (existing subcommands)."""
    parser = argparse.ArgumentParser(
        prog="autofix",
        description=(
            "Deterministic, git-scoped codebase scanner. Reads a commit-range "
            "changeset and emits SARIF + envelope-compatible events.jsonl rows."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="subcommand",
        metavar="<subcommand>",
        required=False,
    )

    scan_parser = subparsers.add_parser(
        "scan",
        help="Run a single scan over the current changeset.",
        description=scan_command.HELP_DESCRIPTION,
        epilog=scan_command.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scan_command.add_arguments(scan_parser)
    scan_parser.set_defaults(_runner=scan_command.run)

    fix_parser = subparsers.add_parser(
        "fix",
        help="Apply autofix's deterministic fixes (default dry-run; --apply to write).",
        description=fix_command.HELP_DESCRIPTION,
        epilog=fix_command.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fix_command.add_arguments(fix_parser)
    fix_parser.set_defaults(_runner=fix_command.run)

    run_parser = subparsers.add_parser(
        "run",
        help="Drive the full workflow loop (scan → triage → plan → apply → verify).",
        description=run_command.HELP_DESCRIPTION,
        epilog=run_command.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_command.add_arguments(run_parser)
    run_parser.set_defaults(_runner=run_command.run)

    replay_parser = subparsers.add_parser(
        "replay",
        help="Replay a past scan from .autofix/events.jsonl (no LLM, no writes).",
        description=(
            "Replay a previously recorded scan deterministically. Re-runs the "
            "funnel pipeline under replay_mode() against the ChangeSet "
            "rehydrated from the ScanStarted anchor and diffs the reproduced "
            "findings against the historical ones. Exit codes: 0 match, "
            "1 mismatch/version_drift, 2 missing_anchor or input error."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    replay_command.add_arguments(replay_parser)
    replay_parser.set_defaults(_runner=replay_command.run)

    export_sarif_parser = subparsers.add_parser(
        "export-sarif",
        help="Export a SARIF 2.1.0 file from a previously recorded scan.",
        description=(
            "Reconstruct a SARIF 2.1.0 document from the .autofix/events.jsonl "
            "rows of a past scan (identified by --scan-id). Writes to --out "
            "when supplied; streams to stdout otherwise. Exit codes: 0 success, "
            "1 scan_id / events.jsonl missing, 2 OSError or emit failure, "
            "3 SARIF shape-check failure."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    export_sarif_command.add_arguments(export_sarif_parser)
    export_sarif_parser.set_defaults(_runner=export_sarif_command.run)

    watch_parser = subparsers.add_parser(
        "watch",
        help="Watchman-backed long-running scanner.",
        description=watch_command.HELP_DESCRIPTION,
        epilog=watch_command.HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    watch_command.add_arguments(watch_parser)
    watch_parser.set_defaults(_runner=watch_command.run)

    policy_parser = subparsers.add_parser(
        "policy",
        help="Inspect or validate the locked .autofix/autofix-policy.json.",
        description=policy_command.HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    policy_command.add_arguments(policy_parser)
    policy_parser.set_defaults(_runner=policy_command.run)

    return parser


def _build_advanced_help() -> str:
    """Render the full subcommand + flag reference."""
    parser = _build_parser()
    full = parser.format_help()
    bare_flags = (
        "\n"
        "BARE-AUTOFIX (continuous crawl, foreground) FLAGS:\n"
        "  --root PATH            Repository to scan (required for crawl)\n"
        "  --apply                Apply fixes (overrides config mode=preview)\n"
        "  --once                 Run one cycle, then exit (no continuous loop)\n"
        "\n"
        "TOP-LEVEL SUBCOMMANDS:\n"
        "  autofix init           Set up autofix for this repo (one-time)\n"
        "  autofix start          Daemonize the crawl (background, forever)\n"
        "  autofix status         Show what autofix is doing right now\n"
        "  autofix logs           Tail .autofix/daemon.log\n"
        "  autofix stop           Send SIGTERM to the running daemon\n"
    )
    return full + bare_flags


def _dispatch_init(argv: list[str]) -> int:
    """Parse ``autofix init`` flags, run the wizard."""
    parser = argparse.ArgumentParser(prog="autofix init")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    return init_command.run_init(default_root=args.root)


def _dispatch_status(argv: list[str]) -> int:
    """Parse ``autofix status`` flags, print the summary."""
    parser = argparse.ArgumentParser(prog="autofix status")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    return status_command.run_status(root=args.root)


def _dispatch_start(argv: list[str]) -> int:
    """Parse ``autofix start`` flags, daemonize the crawl."""
    parser = argparse.ArgumentParser(prog="autofix start")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    return start_command.run_start(root=args.root)


def _dispatch_stop(argv: list[str]) -> int:
    """Parse ``autofix stop`` flags, signal the daemon to exit."""
    parser = argparse.ArgumentParser(prog="autofix stop")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    return stop_command.run_stop(root=args.root)


def _dispatch_logs(argv: list[str]) -> int:
    """Parse ``autofix logs`` flags, stream the daemon log."""
    parser = argparse.ArgumentParser(prog="autofix logs")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    logs_command.add_arguments(parser)
    args = parser.parse_args(argv)
    return logs_command.run_logs(
        root=args.root, lines=args.lines, follow=args.follow,
    )


def _dispatch_bare_crawl(argv: list[str]) -> int:
    """Parse bare-``autofix`` flags and drive the continuous crawl."""
    from autofix.crawl import driver
    from autofix.crawl.config import read_config, resolve_budget_tier
    from autofix.crawl.crawl_constants import (
        MODE_COMMIT,
        MODE_PREVIEW,
    )

    parser = argparse.ArgumentParser(prog="autofix")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.root is None:
        # No subcommand and no --root → print the dumb-user help.
        print(_DUMB_USER_HELP)
        return 0

    config = read_config(args.root)
    mode = config["mode"]
    budget = config["budget"]
    if args.apply and mode == MODE_PREVIEW:
        # --apply overrides the config's preview mode → upgrade to commit.
        # (The operator can switch to ``pr`` via ``autofix init`` if they
        # want PR creation instead.)
        mode = MODE_COMMIT

    interval = resolve_budget_tier(budget)["interval_seconds"]

    if args.once:
        return driver.run_crawl_once(
            root=args.root, mode=mode, budget=budget, quiet=args.quiet,
        )
    return driver.run_crawl_continuously(
        root=args.root, mode=mode, budget=budget,
        interval_seconds=interval, quiet=args.quiet,
    )


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and dispatch.

    Routing:
    * ``autofix --help`` (or no args) → dumb-user help, exit 0.
    * ``autofix --help-advanced`` → full subcommand + flag reference.
    * ``autofix init`` / ``status`` / ``start`` / ``stop`` / ``logs``
      → top-level daemon commands.
    * ``autofix scan|fix|run|replay|export-sarif|watch|policy`` → argparse.
    * ``autofix --root <p> [--apply] [--once]`` → continuous crawl
      (foreground; ``start`` is the daemonized form).
    """
    if argv is None:
        argv = sys.argv[1:]
    elif argv and argv[0] == "autofix":
        # Tests sometimes pass the program name as argv[0]; strip it.
        argv = argv[1:]

    # Layered help — handle BEFORE argparse sees the args.
    if not argv or argv == ["--help"] or argv == ["-h"]:
        print(_DUMB_USER_HELP)
        return 0
    if argv[0] == "--help-advanced":
        print(_build_advanced_help())
        return 0

    # New top-level commands.
    if argv[0] == "init":
        return _dispatch_init(argv[1:])
    if argv[0] == "status":
        return _dispatch_status(argv[1:])
    if argv[0] == "start":
        return _dispatch_start(argv[1:])
    if argv[0] == "stop":
        return _dispatch_stop(argv[1:])
    if argv[0] == "logs":
        return _dispatch_logs(argv[1:])

    # Bare-autofix path: first arg starts with `--` (a flag) → crawl.
    if argv[0].startswith("--"):
        return _dispatch_bare_crawl(argv)

    # Existing subcommand (argparse).
    if argv[0] in _ARGPARSE_SUBCOMMANDS:
        parser = _build_parser()
        args = parser.parse_args(argv)
        runner = getattr(args, "_runner", None)
        if runner is None:
            print(_DUMB_USER_HELP)
            return 0
        return int(runner(args))

    # Unknown first arg — fall back to argparse for a uniform error.
    parser = _build_parser()
    args = parser.parse_args(argv)
    runner = getattr(args, "_runner", None)
    if runner is None:
        print(_DUMB_USER_HELP)
        return 0
    return int(runner(args))


if __name__ == "__main__":  # pragma: no cover - exercised via console_script
    sys.exit(main())


__all__ = ["main"]
