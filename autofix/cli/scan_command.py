"""The ``autofix scan`` subcommand.

Argparse glue around :func:`autofix.scan_core._run_scan_core`. The
pipeline body lives in ``autofix.scan_core`` so non-cli callers
(:mod:`autofix.cli.run_command`, :mod:`autofix.workflow.verify`) can
invoke it without depending on ``autofix.cli``.

Pipeline (in :mod:`autofix.scan_core`):

1. :func:`autofix.events.change_detector.detect` turns
   ``--root`` + ``--full-sweep`` into a :class:`ChangeSet` + confidence
   label (``diff-head1``, ``full-sweep``, or ``full-sweep-fallback``).
2. An initial ``ScanStarted`` envelope row is appended to
   ``<root>/.autofix/events.jsonl``.
3. :func:`autofix.funnel.pipeline.run_scan` walks the changeset,
   parses each file, builds :class:`EvidencePacket` s, and schedules
   each via the (single) LLM seam.
4. :func:`autofix.telemetry.sarif.emit_sarif` writes the
   deterministic SARIF 2.1.0 document under
   ``<root>/.autofix/scans-next/<scan-id>/findings.sarif``.
5. ``SARIFEmitted`` + ``ScanCompleted`` envelope rows are appended.

Working-tree edits are ignored on purpose — the changeset is strictly
commit-to-commit. This is what ``--help`` advertises (AC #25).
"""

from __future__ import annotations

import argparse
from pathlib import Path

# Re-exports for backward compatibility. Callers (and tests) that import
# private helpers from the historical location continue to work; the
# canonical home is ``autofix.scan_core``.
from autofix.scan_core import (
    _SCAN_ID_PATTERN,
    ScanCoreResult,
    _compute_policy_sha256,
    _mint_scan_id,
    _print_summary,
    _resolve_commit_sha,
    _run_scan_core,
    _safe_append,
    _safe_append_with_id,
    _validate_scan_id,
)

HELP_DESCRIPTION: str = (
    "Run a single autofix scan over the current changeset.\n"
    "\n"
    "The change set is derived from a git diff range (default: HEAD~1..HEAD),\n"
    "filtered to *.py files. Working-tree modifications are ignored: only\n"
    "committed changes are scanned. Commit your edits before re-running to\n"
    "see them reflected."
)


HELP_EPILOG: str = (
    "Determinism notes:\n"
    "  * The change set comes from a git diff range — working-tree\n"
    "    modifications are ignored; commit first to include them.\n"
    "  * Default range is HEAD~1..HEAD. --full-sweep scans every tracked\n"
    "    *.py via 'git ls-files'. Single-commit repos fall back to a full\n"
    "    sweep automatically (watcher_confidence='full-sweep-fallback').\n"
    "  * Every envelope row is appended to .autofix/events.jsonl; replay\n"
    "    from .autofix/events.jsonl reconstructs the full scan timeline,\n"
    "    which is the supported debugging path for CI failures.\n"
    "  * SARIF is written deterministically (sorted keys, indent=2) to\n"
    "    .autofix/scans-next/<scan-id>/findings.sarif.\n"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``scan``'s flags onto an argparse (sub)parser.

    Separated from :func:`run` so tests can introspect the flag surface
    without invoking the scan.
    """
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="Repository root to scan (must be inside a git working tree).",
    )
    parser.add_argument(
        "--full-sweep",
        action="store_true",
        help=(
            "Ignore the diff range and scan every tracked *.py. Sets "
            "watcher_confidence='full-sweep'."
        ),
    )
    parser.add_argument(
        "--fresh-instance",
        action="store_true",
        help=(
            "Force the planner into a bounded full sweep over known graph "
            "symbols (bypasses caller-graph traversal). Useful for "
            "cold-start, forced re-index, and testing. Sets "
            "ChangeSet.is_fresh_instance=True regardless of the watcher "
            "confidence label."
        ),
    )
    parser.add_argument(
        "--scan-id",
        type=str,
        default=None,
        help=(
            "Explicit scan id. Defaults to "
            "<UTC-timestamp>-<8-hex-chars> generated at invocation time."
        ),
    )
    parser.add_argument(
        "--analyzers",
        type=str,
        default="",
        help=(
            "Comma-separated list of analyzer set names "
            "(e.g. 'cheap,linter:ruff'). Empty/missing means "
            "today's default (only 'cheap' runs)."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Suppress per-stage progress lines on stderr. The final "
            "summary (and any errors) are still printed."
        ),
    )


def run(args: argparse.Namespace) -> int:
    """Argparse-driven entry point for ``autofix scan``.

    Thin wrapper around :func:`autofix.scan_core._run_scan_core`. All
    pipeline logic lives in the core function so the orchestrator in
    :mod:`autofix.cli.run_command` and the workflow verify pass in
    :mod:`autofix.workflow.verify` can invoke it directly without
    spawning a subprocess and without doubling the
    ``ScanStarted``/``ScanCompleted`` event pair.
    """
    raw_analyzers = getattr(args, "analyzers", "") or ""
    parts = [p.strip() for p in raw_analyzers.split(",") if p.strip()]
    analyzer_set = parts if parts else None
    result = _run_scan_core(
        root=args.root,
        full_sweep=bool(args.full_sweep),
        analyzer_set=analyzer_set,
        scan_id=args.scan_id,
        fresh_instance=bool(getattr(args, "fresh_instance", False)),
        quiet=bool(getattr(args, "quiet", False)),
    )
    return result.exit_code


__all__ = [
    "HELP_DESCRIPTION",
    "HELP_EPILOG",
    "ScanCoreResult",
    "add_arguments",
    "run",
    # Re-exported from autofix.scan_core for back-compat with code/tests
    # that historically imported these private helpers from this module.
    "_run_scan_core",
    "_mint_scan_id",
    "_validate_scan_id",
    "_safe_append",
    "_safe_append_with_id",
    "_resolve_commit_sha",
    "_compute_policy_sha256",
    "_print_summary",
    "_SCAN_ID_PATTERN",
]
