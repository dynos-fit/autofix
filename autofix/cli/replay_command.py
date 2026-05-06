"""The ``autofix replay`` subcommand (ACs 46-49).

Replays a past scan identified by ``--scan-id`` by delegating to
:func:`autofix.telemetry.replay.replay`. The ``replay`` engine parses
``<root>/.autofix/events.jsonl`` for the matching ``ScanStarted`` anchor,
rehydrates the :class:`ChangeSet` from the anchor's ``extra`` block, re-runs
the funnel pipeline inside :func:`replay_mode` (no git checkout, no LLM
call), and classifies the outcome into one of four verdicts:

* ``match`` — historical and reproduced candidate sets align, and every
  paired finding's prompt-prefix hash recomputes byte-identically.
* ``mismatch`` — at least one of: a finding exists on one side only, or a
  paired finding's prompt-prefix hash differs.
* ``missing_anchor`` — no ``ScanStarted`` row carrying ``scan_id`` was
  found in ``events.jsonl``.
* ``version_drift`` — the anchor is present but at least one of
  ``policy_sha256``, ``analyzer_version``, ``tree_sitter_version`` differs
  from the current environment; ``drift_reason`` is populated.

CLI contract (AC 46-49):

* ``--scan-id`` is required.
* ``--root`` defaults to :func:`Path.cwd`.
* ``--json`` is a ``store_true`` flag; when set, stdout receives
  ``json.dumps(dataclasses.asdict(scan_run), sort_keys=True, indent=2)``
  with tuples rendered as lists.
* Without ``--json``, stdout receives a <=20 line human-readable summary
  (verdict, scan_id, commit_sha, the six counts, optional drift_reason,
  and up to 5 mismatched finding IDs).
* Exit codes: 0 on ``match``; 1 on ``mismatch`` or ``version_drift``;
  2 on ``missing_anchor`` or on ``ValueError`` / ``FileNotFoundError``
  raised out of :func:`replay`.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from autofix.telemetry.replay import ScanRun, replay


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Wire the ``replay`` subparser with its three arguments (AC 46).

    Parameters
    ----------
    parser:
        The subparser returned by ``subparsers.add_parser("replay", ...)``
        in :mod:`autofix.cli.main`. This function mutates it in
        place, matching the shape used by
        :func:`autofix.cli.scan_command.add_arguments`.
    """
    parser.add_argument(
        "--scan-id",
        required=True,
        help="scan id to replay (from .autofix/events.jsonl).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root containing .autofix/ (default: current directory).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "emit the full ScanRun as JSON (sorted keys, indent=2) instead "
            "of the human-readable summary."
        ),
    )


def run(args: argparse.Namespace) -> int:
    """Execute the replay and return the process exit code (ACs 47-49).

    The error-to-exit-code mapping is pinned by AC 49:

    * ``verdict == "match"``            -> 0
    * ``verdict in {"mismatch",
      "version_drift"}``                -> 1
    * ``verdict == "missing_anchor"``   -> 2
    * ``ValueError`` / ``FileNotFoundError`` from :func:`replay`
                                         -> 2 (diagnostic on stderr)

    Any other verdict string is treated as a mismatch (exit 1) — this
    branch is defensive; :mod:`autofix.telemetry.replay` only
    produces the four verdicts above.
    """
    # ``--root`` defaults to the process cwd (AC 46). ``Path.cwd()`` is
    # evaluated here, not at parser-construction time, so tests that
    # chdir into a tmp path pick up the right root.
    root = args.root if args.root is not None else Path.cwd()

    try:
        scan_run = replay(args.scan_id, root)
    except ValueError as exc:
        # AC 36 + AC 49: empty / non-string scan_id. Map to exit 2.
        print(f"autofix replay: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        # AC 36 + AC 49: events.jsonl missing. Map to exit 2.
        print(f"autofix replay: {exc}", file=sys.stderr)
        return 2

    if args.json:
        # AC 48. ``dataclasses.asdict`` recurses into nested dataclasses
        # (HistoricalFinding, CandidateFinding) and renders tuples as
        # lists via json.dumps's default tuple handling.
        try:
            payload = dataclasses.asdict(scan_run)
            serialized = json.dumps(payload, sort_keys=True, indent=2, default=_json_default)
        except (TypeError, ValueError) as exc:
            # Serialization failure is a programming bug — treat as a
            # hard error rather than silently degrading to the text
            # summary, so the failure is noisy.
            print(
                f"autofix replay: failed to serialize ScanRun: {exc}",
                file=sys.stderr,
            )
            return 2
        print(serialized)
    else:
        _print_summary(scan_run)

    # AC 49 exit-code mapping.
    verdict = scan_run.verdict
    if verdict == "match":
        return 0
    if verdict in ("mismatch", "version_drift"):
        return 1
    if verdict == "missing_anchor":
        return 2
    # Defensive fallback — Literal narrowing in replay.py prevents this
    # in practice, but we don't want an unknown verdict to silently
    # succeed.
    return 1


def _json_default(obj: Any) -> Any:
    """JSON fallback for objects ``dataclasses.asdict`` did not flatten.

    Tuples and lists round-trip natively; this hook catches :class:`Path`
    and any other non-primitive that slipped through (for example, if a
    future ``ScanRun`` field carries a non-dataclass object). Unknown
    types fall back to ``repr`` so the JSON output is always valid.
    """
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if hasattr(obj, "__dataclass_fields__"):
        return dataclasses.asdict(obj)
    return repr(obj)


def _print_summary(sr: ScanRun) -> None:
    """Emit the default <=20 line human summary to stdout (AC 47).

    Line budget breakdown (worst case):

    * 1 verdict
    * 1 scan_id
    * 1 commit_sha
    * 1 counts line (historical/reproduced/matched)
    * 1 counts line (historical_only/reproduced_only/hash_mismatched)
    * 1 drift_reason (only when present)
    * 1 "mismatched finding ids (first 5):" header
    * up to 5 mismatched finding id lines

    Max total: 11 lines. Well under the 20-line cap. The cap is still
    applied defensively so a future field addition cannot silently blow
    past it.
    """
    lines: list[str] = [
        f"verdict: {sr.verdict}",
        f"scan_id: {sr.scan_id}",
        f"commit_sha: {sr.commit_sha}",
        (
            f"counts: historical={len(sr.historical_findings)} "
            f"reproduced={len(sr.reproduced_findings)} "
            f"matched={len(sr.matched)}"
        ),
        (
            f"        historical_only={len(sr.historical_only)} "
            f"reproduced_only={len(sr.reproduced_only)} "
            f"prompt_prefix_hash_mismatched={len(sr.prompt_prefix_hash_mismatched)}"
        ),
    ]
    if sr.drift_reason:
        lines.append(f"drift_reason: {sr.drift_reason}")
    if sr.prompt_prefix_hash_mismatched:
        lines.append("mismatched finding ids (first 5):")
        for triple in sr.prompt_prefix_hash_mismatched[:5]:
            # triple = (historical_finding_id, historical_hash, reproduced_hash)
            # AC 47 asks for finding IDs only on their own line.
            lines.append(f"  - {triple[0]}")

    for line in lines[:20]:
        print(line)


__all__ = ["add_arguments", "run"]
