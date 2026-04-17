"""Top-level entry for the ``autofix-next`` console script.

Wires a single-level argparse with one subcommand today (``scan``). New
subcommands should be added by importing their ``add_arguments`` /
``run`` callables from a dedicated module under ``autofix_next/cli/`` and
registering them below — the dispatch stays flat, no global state.
"""

from __future__ import annotations

import argparse
import sys

from autofix_next.cli import scan_command


def _build_parser() -> argparse.ArgumentParser:
    """Assemble the top-level parser with every registered subcommand.

    Kept as a function (not a module-level constant) so tests can
    construct a fresh parser in isolation without import-time side
    effects.
    """
    parser = argparse.ArgumentParser(
        prog="autofix-next",
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
        # left ``required=False`` to allow ``autofix-next --help`` to
        # print the top-level help without demanding a subcommand first.
        parser.print_help(sys.stderr)
        return 2

    return int(runner(args))


if __name__ == "__main__":  # pragma: no cover - exercised via console_script
    sys.exit(main())


__all__ = ["main"]
