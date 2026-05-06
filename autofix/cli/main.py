"""Top-level entry for the ``autofix`` console script.

Wires a single-level argparse with one subcommand today (``scan``). New
subcommands should be added by importing their ``add_arguments`` /
``run`` callables from a dedicated module under ``autofix/cli/`` and
registering them below — the dispatch stays flat, no global state.
"""

from __future__ import annotations

import argparse
import sys

from autofix.cli import (
    export_sarif_command,
    fix_command,
    policy_command,
    replay_command,
    scan_command,
    watch_command,
)


def _build_parser() -> argparse.ArgumentParser:
    """Assemble the top-level parser with every registered subcommand.

    Kept as a function (not a module-level constant) so tests can
    construct a fresh parser in isolation without import-time side
    effects.
    """
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


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the requested subcommand.

    Parameters
    ----------
    argv:
        Argument vector (excluding ``argv[0]``). When ``None``, defaults
        to :data:`sys.argv` ``[1:]`` — mirroring argparse's default.

    Returns
    -------
    int
        Process exit code. ``0`` on success, ``1`` on scan/runtime error,
        ``2`` on usage error (unknown/missing subcommand).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    runner = getattr(args, "_runner", None)
    if runner is None:
        # No subcommand was supplied. argparse won't error because we
        # left ``required=False`` to allow ``autofix --help`` to
        # print the top-level help without demanding a subcommand first.
        parser.print_help(sys.stderr)
        return 2

    return int(runner(args))


if __name__ == "__main__":  # pragma: no cover - exercised via console_script
    sys.exit(main())


__all__ = ["main"]
# task-20260506-003: package directory renamed (single CLI namespace)
