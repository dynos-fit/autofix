"""The ``autofix-next scan`` subcommand.

Pipeline:

1. :func:`autofix_next.events.change_detector.detect` turns
   ``--root`` + ``--full-sweep`` into a :class:`ChangeSet` + confidence
   label (``diff-head1``, ``full-sweep``, or ``full-sweep-fallback``).
2. An initial ``ScanStarted`` envelope row is appended to
   ``<root>/.autofix/events.jsonl``.
3. :func:`autofix_next.funnel.pipeline.run_scan` walks the changeset,
   parses each file, builds :class:`EvidencePacket` s, and schedules
   each via the (single) LLM seam.
4. :func:`autofix_next.telemetry.sarif.emit_sarif` writes the
   deterministic SARIF 2.1.0 document under
   ``<root>/.autofix-next/scans/<scan-id>/findings.sarif``.
5. ``SARIFEmitted`` + ``ScanCompleted`` envelope rows are appended.

Working-tree edits are ignored on purpose — the changeset is strictly
commit-to-commit. This is what ``--help`` advertises (AC #25).
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Scan-id must match this shape: alphanumeric, dot, underscore, hyphen only;
# 1–128 chars. Rejects path-traversal sequences (``..``), absolute paths
# (leading ``/``), URL schemes (``file://``, ``://``), and shell metacharacters.
# Addresses SEC-01: arbitrary-file-write via --scan-id path traversal.
_SCAN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

from autofix_next.events.change_detector import (
    GitUnavailableError,
    NotAGitRepoError,
    detect,
)
from autofix_next.events.ingress import ingest_cli_invocation
from autofix_next.funnel.pipeline import run_scan
from autofix_next.telemetry import events_log
from autofix_next.telemetry.sarif import emit_sarif


HELP_DESCRIPTION: str = (
    "Run a single autofix-next scan over the current changeset.\n"
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
    "    .autofix-next/scans/<scan-id>/findings.sarif.\n"
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


def _mint_scan_id() -> str:
    """Return a default ``scan_id`` of ``YYYYMMDDTHHMMSSZ-<8hex>``.

    The timestamp second resolution is a concession: two scans started
    in the same second in the same process would otherwise collide. The
    4 random bytes appended as hex make that vanishingly unlikely.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{os.urandom(4).hex()}"


def _validate_scan_id(scan_id: str) -> None:
    """Raise ``ValueError`` unless ``scan_id`` matches the allowlist.

    Blocks path-traversal (``..``), absolute paths, and URL schemes
    from flowing into ``root / ".autofix-next" / "scans" / scan_id``.
    """
    if not _SCAN_ID_PATTERN.fullmatch(scan_id):
        raise ValueError(
            f"invalid --scan-id {scan_id!r}: must match [A-Za-z0-9._-]{{1,128}}"
        )


def _safe_append(
    root: Path, event_type: str, payload: dict
) -> None:
    """Append an envelope row, swallowing IO errors.

    Telemetry loss must not abort the scan (same contract as
    ``funnel.pipeline._emit_packet_built_event`` and
    ``llm.scheduler._emit_gated_event``).
    """
    try:
        events_log.append_event(root, event_type, payload)
    except OSError:
        pass
    except ValueError:
        # Unknown event name: a programming error in this file. Re-raise
        # so it surfaces during development rather than hiding in prod.
        raise


def run(args: argparse.Namespace) -> int:
    """Execute the scan subcommand; return a process exit code.

    Any unexpected exception mid-pipeline is caught, a ``ScanCompleted``
    row with an error note is emitted (best-effort), and the caller gets
    a non-zero exit code. We deliberately do NOT propagate; console
    scripts should turn exceptions into exit codes.
    """
    root: Path = args.root
    full_sweep: bool = bool(args.full_sweep)
    scan_id: str = args.scan_id or _mint_scan_id()

    # SEC-01: reject path-traversal / absolute-path scan_id before it reaches
    # the filesystem. Auto-minted ids always match the pattern; user-supplied
    # ids are validated here so a malicious --scan-id cannot escape the
    # ``.autofix-next/scans/`` directory.
    try:
        _validate_scan_id(scan_id)
    except ValueError as exc:
        print(f"autofix-next: {exc}", file=sys.stderr)
        return 2

    # --- 1. Change detection -------------------------------------------------
    try:
        changeset, watcher_confidence = detect(root, full_sweep=full_sweep)
    except (GitUnavailableError, NotAGitRepoError) as exc:
        print(f"autofix-next: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        print(
            f"autofix-next: change detection failed: {exc}",
            file=sys.stderr,
        )
        return 1

    # AC #3: --fresh-instance forces the bounded full-sweep fast path on
    # the planner regardless of what the detector inferred. ``dataclasses.
    # replace`` preserves the other ChangeSet fields (paths,
    # watcher_confidence) and goes through ``__post_init__`` for
    # validation.
    if getattr(args, "fresh_instance", False):
        changeset = dataclasses.replace(changeset, is_fresh_instance=True)

    # --- 2. Ingest + ScanStarted event --------------------------------------
    event = ingest_cli_invocation(
        root,
        full_sweep=full_sweep,
        scan_id=scan_id,
        watcher_confidence=watcher_confidence,
    )
    scan_started_payload = event.to_payload()
    scan_started_payload["path_count"] = len(changeset.paths)
    _safe_append(root, "ScanStarted", scan_started_payload)

    # --- 3. Funnel -----------------------------------------------------------
    try:
        result = run_scan(root, changeset, scan_id)
    except Exception as exc:
        traceback_str = traceback.format_exc()
        print(
            f"autofix-next: scan failed: {exc}\n{traceback_str}",
            file=sys.stderr,
        )
        _safe_append(
            root,
            "ScanCompleted",
            {
                "event_type": "ScanCompleted",
                "repo_id": root.name,
                "scan_id": scan_id,
                "watcher_confidence": watcher_confidence,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return 1

    # --- 4. SARIF ------------------------------------------------------------
    sarif_path = (
        root / ".autofix-next" / "scans" / scan_id / "findings.sarif"
    )
    # SEC-01 defense-in-depth: the scan_id validator above blocks ``..`` and
    # absolute paths, but verify containment once more after path composition
    # so a future refactor cannot silently reintroduce escape.
    try:
        sarif_path.resolve().relative_to(root.resolve())
    except ValueError:
        print(
            f"autofix-next: refused to write SARIF outside {root}: {sarif_path}",
            file=sys.stderr,
        )
        return 1
    try:
        emit_sarif(scan_id, result.findings, sarif_path)
    except Exception as exc:
        print(
            f"autofix-next: SARIF emit failed: {exc}",
            file=sys.stderr,
        )
        _safe_append(
            root,
            "ScanCompleted",
            {
                "event_type": "ScanCompleted",
                "repo_id": root.name,
                "scan_id": scan_id,
                "watcher_confidence": watcher_confidence,
                "status": "error",
                "error": f"sarif: {type(exc).__name__}: {exc}",
            },
        )
        return 1

    _safe_append(
        root,
        "SARIFEmitted",
        {
            "event_type": "SARIFEmitted",
            "repo_id": root.name,
            "scan_id": scan_id,
            "sarif_path": str(sarif_path),
            "finding_count": len(result.findings),
        },
    )

    _safe_append(
        root,
        "ScanCompleted",
        {
            "event_type": "ScanCompleted",
            "repo_id": root.name,
            "scan_id": scan_id,
            "watcher_confidence": watcher_confidence,
            "status": "ok",
            "finding_count": len(result.findings),
        },
    )

    return 0


__all__ = ["HELP_DESCRIPTION", "HELP_EPILOG", "add_arguments", "run"]
