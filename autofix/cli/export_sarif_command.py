"""The ``autofix export-sarif`` subcommand (ACs 1-7, 14, 15, 25).

Exports a SARIF 2.1.0 document reconstructed from a previously recorded
scan in ``<root>/.autofix/events.jsonl``. The subcommand locates the
``ScanStarted`` anchor that matches ``--scan-id``, rebuilds a list of
finding dicts from every ``CandidateFindingProduced`` row sharing that
scan id, and delegates the emission to
:func:`autofix.telemetry.sarif.emit_sarif`.

CLI contract (AC 2):

* ``--scan-id`` is required.
* ``--root`` defaults to :func:`Path.cwd` (resolved at run time, not at
  parser-construction time, so tests that chdir into a tmp dir work).
* ``--out`` defaults to ``None``; when unset, the SARIF document is
  streamed to stdout via a best-effort temp file (AC 6, 7).

Exit codes (AC 3):

* ``0`` on success.
* ``1`` when ``events.jsonl`` is missing or when no ``ScanStarted`` row
  matching ``scan_id`` is present.
* ``2`` on ``OSError`` while reading ``events.jsonl`` or while
  :func:`emit_sarif` fails to write the SARIF document.
* ``3`` reserved for SARIF shape-check failures surfaced by
  :func:`emit_sarif` as :class:`TypeError` / :class:`ValueError` (AC 3).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from autofix.telemetry.sarif import emit_sarif


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Wire the ``export-sarif`` subparser with its three flags (AC 2).

    Parameters
    ----------
    parser:
        The subparser returned by
        ``subparsers.add_parser("export-sarif", ...)`` in
        :mod:`autofix.cli.main`. Mutated in place, matching the
        shape used by :func:`autofix.cli.replay_command.add_arguments`.
    """
    parser.add_argument(
        "--scan-id",
        required=True,
        type=str,
        help="scan id to export (from .autofix/events.jsonl).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root containing .autofix/ (default: current directory).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output SARIF file path (default: stdout).",
    )


def run(args: argparse.Namespace) -> int:
    """Execute the export and return the process exit code (ACs 3-7).

    The branching is deliberately explicit so every path maps to an AC:

    * events.jsonl missing                -> 1 (AC 3, AC 4)
    * ScanStarted anchor missing          -> 1 (AC 4)
    * OSError reading events.jsonl        -> 2 (AC 3)
    * emit_sarif raises OSError           -> 2 (AC 3)
    * emit_sarif raises TypeError/ValueError -> 3 (AC 3)
    * otherwise                           -> 0 (AC 3)
    """
    # AC 2: --root defaults to cwd, resolved here so tests that chdir
    # into a tmp directory pick up the right root.
    root = args.root if args.root is not None else Path.cwd()
    events_path = root / ".autofix" / "events.jsonl"

    if not events_path.is_file():
        # AC 4: diagnostic mentions the full path to aid debugging.
        print(
            f"autofix export-sarif: events.jsonl not found at {events_path}",
            file=sys.stderr,
        )
        return 1

    scan_id = args.scan_id
    anchor_found = False
    findings: list[dict] = []

    try:
        raw = events_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"autofix export-sarif: error reading {events_path}: {exc}",
            file=sys.stderr,
        )
        return 2

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # Skip malformed lines defensively — events.jsonl is
            # append-only and a torn write should not abort export.
            continue
        if not isinstance(row, dict):
            continue

        # Envelope shape varies slightly across emitters; accept both a
        # nested ``payload`` block and a flat row.
        payload = row.get("payload")
        if not isinstance(payload, dict):
            payload = row.get("scan_event") if isinstance(row.get("scan_event"), dict) else row

        if payload.get("scan_id") != scan_id:
            continue

        event_type = (
            row.get("event_type")
            or row.get("event")
            or payload.get("event_type")
            or payload.get("event")
        )
        if event_type == "ScanStarted":
            anchor_found = True
        elif event_type == "CandidateFindingProduced":
            # AC 5: reconstruct the minimal finding dict shape expected
            # by emit_sarif. Keys absent from the payload fall back to
            # safe defaults so emit_sarif's shape check does not trip.
            start_line = payload.get("start_line", 1)
            finding: dict = {
                "rule_id": payload.get("rule_id", ""),
                "path": payload.get("path") or payload.get("relpath", ""),
                "symbol_name": payload.get("symbol_name", ""),
                "finding_id": payload.get("finding_id", ""),
                "start_line": start_line,
                "end_line": payload.get("end_line", start_line),
            }
            if "normalized_import" in payload:
                finding["normalized_import"] = payload["normalized_import"]
            findings.append(finding)

    if not anchor_found:
        # AC 4: exact wording — ``scan_id {scan_id!r} not found in {events_path}``.
        print(
            f"autofix export-sarif: scan_id {scan_id!r} not found in {events_path}",
            file=sys.stderr,
        )
        return 1

    # AC 6: target path is a temp file when --out is None, else the
    # caller-supplied path.
    if args.out is None:
        try:
            with tempfile.NamedTemporaryFile(suffix=".sarif", delete=False) as tmp:
                target_path = Path(tmp.name)
        except OSError as exc:
            print(
                f"autofix export-sarif: could not create temp file: {exc}",
                file=sys.stderr,
            )
            return 2
        try:
            emit_sarif(scan_id, findings, target_path)
            # AC 7: stream to stdout, then best-effort unlink.
            sys.stdout.write(target_path.read_text(encoding="utf-8"))
        except OSError as exc:
            print(
                f"autofix export-sarif: emit failed: {exc}",
                file=sys.stderr,
            )
            return 2
        except (TypeError, ValueError) as exc:
            # AC 3: SARIF shape-check failure.
            print(
                f"autofix export-sarif: SARIF shape check failed: {exc}",
                file=sys.stderr,
            )
            return 3
        finally:
            try:
                target_path.unlink()
            except (FileNotFoundError, OSError):
                # Best-effort cleanup — a stuck temp file must not fail
                # an otherwise-successful export.
                pass
    else:
        try:
            emit_sarif(scan_id, findings, Path(args.out))
        except OSError as exc:
            print(
                f"autofix export-sarif: emit failed: {exc}",
                file=sys.stderr,
            )
            return 2
        except (TypeError, ValueError) as exc:
            # AC 3: SARIF shape-check failure.
            print(
                f"autofix export-sarif: SARIF shape check failed: {exc}",
                file=sys.stderr,
            )
            return 3

    return 0


__all__ = ["add_arguments", "run"]
