"""The ``autofix scan`` subcommand.

Pipeline:

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
import contextlib
import dataclasses
import hashlib
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import tree_sitter

# Scan-id must match this shape: alphanumeric, dot, underscore, hyphen only;
# 1–128 chars. Rejects path-traversal sequences (``..``), absolute paths
# (leading ``/``), URL schemes (``file://``, ``://``), and shell metacharacters.
# Addresses SEC-01: arbitrary-file-write via --scan-id path traversal.
_SCAN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

from autofix.analyzers.cheap import unused_import as _unused_import
from autofix.events.change_detector import (
    GitUnavailableError,
    NotAGitRepoError,
    detect,
)
from autofix.events.ingress import ingest_cli_invocation
from autofix.funnel.pipeline import run_scan
from autofix.telemetry import events_log
from autofix.telemetry.correlation import (
    set_commit_sha,
    set_event_id,
    set_scan_id,
)
from autofix.telemetry.sarif import emit_sarif


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
    from flowing into ``root / ".autofix" / "scans" / scan_id``.
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


def _safe_append_with_id(
    root: Path, event_type: str, payload: dict
) -> str | None:
    """Append an envelope row and return the generated event id.

    Same OSError-swallow discipline as :func:`_safe_append`, but returns
    the ``event_id`` that :func:`events_log.append_event` wrote into the
    row so the caller can stamp it into the correlation contextvar
    (:func:`autofix.telemetry.correlation.set_event_id`). Returns
    ``None`` on IO failure so the caller can fall back to a no-op
    contextvar setter (:func:`contextlib.nullcontext`).

    ``ValueError`` on an unknown event name re-raises: that is a
    programming error and must not be silently dropped.
    """
    try:
        return events_log.append_event(root, event_type, payload)
    except OSError:
        return None
    except ValueError:
        raise


def _resolve_commit_sha(root: Path) -> str:
    """Return ``git rev-parse HEAD`` output for ``root``, or ``""`` on failure.

    No exception escapes: a missing git binary, a non-repo ``root``, a
    detached-HEAD repo with zero commits, or a ``git`` subprocess that
    returns non-zero all yield the empty-string sentinel. The empty
    string is what AC #23 expects when ``diff_match`` must be ``False``.

    ``subprocess.run`` is invoked with ``check=False`` and an explicit
    ``text=True`` decoding; the return is ``.stdout.strip()`` so no
    trailing newline contaminates the value.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        # ``git`` binary missing, ``root`` unreadable, or the probe hung.
        # All three map to the "unknown commit sha" sentinel.
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _compute_policy_sha256(root: Path) -> str:
    """Return the SHA-256 hex digest of ``.autofix/autofix-policy.json``.

    Returns empty string when the file is absent or unreadable (AC #29).
    No exception escapes.
    """
    policy_path = root / ".autofix" / "autofix-policy.json"
    try:
        return hashlib.sha256(policy_path.read_bytes()).hexdigest()
    except OSError:
        return ""


def run(args: argparse.Namespace) -> int:
    """Execute the scan subcommand; return a process exit code.

    Any unexpected exception mid-pipeline is caught, a ``ScanCompleted``
    row with an error note is emitted (best-effort), and the caller gets
    a non-zero exit code. We deliberately do NOT propagate; console
    scripts should turn exceptions into exit codes.

    Seg-6 (AC #8): the three correlation contextvars (``scan_id``,
    ``commit_sha``, ``event_id``) are set via nested ``with`` blocks
    through a :class:`contextlib.ExitStack` so their scope covers the
    entire ``run()`` body — including ``run_scan()`` and the SARIF
    emission. ``commit_sha`` is only entered when the ``git rev-parse``
    probe returns a non-empty value; ``event_id`` is only entered when
    ``append_event`` wrote the ``ScanStarted`` row to disk successfully.
    """
    root: Path = args.root
    full_sweep: bool = bool(args.full_sweep)
    scan_id: str = args.scan_id or _mint_scan_id()
    quiet: bool = bool(getattr(args, "quiet", False))
    raw_analyzers = getattr(args, "analyzers", "") or ""
    parts = [p.strip() for p in raw_analyzers.split(",") if p.strip()]
    analyzer_set = parts if parts else None

    def _progress(msg: str) -> None:
        """Emit one stderr progress line unless --quiet was passed."""
        if quiet:
            return
        print(f"autofix: {msg}", file=sys.stderr, flush=True)

    # SEC-01: reject path-traversal / absolute-path scan_id before it reaches
    # the filesystem. Auto-minted ids always match the pattern; user-supplied
    # ids are validated here so a malicious --scan-id cannot escape the
    # ``.autofix/scans-next/`` directory.
    try:
        _validate_scan_id(scan_id)
    except ValueError as exc:
        print(f"autofix: {exc}", file=sys.stderr)
        return 2

    # Seg-6 (AC #8): resolve commit_sha via ``git rev-parse HEAD`` before
    # entering the correlation-contextvar stack. Empty string means the
    # probe failed (not a git repo, no HEAD, git missing) — the setter is
    # then skipped so ``current_commit_sha()`` returns ``None`` downstream
    # and ``diff_match`` will resolve ``False`` (AC #23).
    commit_sha = _resolve_commit_sha(root)

    # Seg-6 (AC #8): stack the three correlation setters so every line
    # below (change detection, ingress, funnel, SARIF emit, completion
    # events) observes the same correlation ids via the contextvar
    # getters. ``scan_id`` is always set; the other two are gated behind
    # their "have a value" precondition and fall back to a no-op context
    # manager so nesting stays symmetric.
    with contextlib.ExitStack() as stack:
        stack.enter_context(set_scan_id(scan_id))
        if commit_sha:
            stack.enter_context(set_commit_sha(commit_sha))

        # --- 1. Change detection -------------------------------------------------
        _progress(
            f"Detecting changes in {root} "
            f"({'full sweep' if full_sweep else 'incremental'})..."
        )
        try:
            changeset, watcher_confidence = detect(root, full_sweep=full_sweep)
        except (GitUnavailableError, NotAGitRepoError) as exc:
            print(f"autofix: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # pragma: no cover - defensive
            print(
                f"autofix: change detection failed: {exc}",
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
        # Seg-6 (AC #23): thread the resolved commit_sha into the ingress
        # event so ``ScanStarted.commit_sha`` matches ``current_commit_sha()``
        # — that equality is what AC #23's ``diff_match`` assertion relies on.
        # The ingress accepts ``commit_sha=None`` as a valid "unknown" sentinel;
        # we pass ``None`` when the probe returned empty so the payload
        # preserves the "missing" shape rather than stamping a literal "".
        event = ingest_cli_invocation(
            root,
            full_sweep=full_sweep,
            scan_id=scan_id,
            commit_sha=commit_sha or None,
            watcher_confidence=watcher_confidence,
        )
        scan_started_payload = event.to_payload()
        scan_started_payload["path_count"] = len(changeset.paths)

        # Seg-6 (AC #29 / AC #30): stamp 5 new determinism-pinning keys onto
        # ``ScanStarted.extra`` BEFORE the row is written. ``policy_sha256``
        # falls back to empty string when the file is absent (AC #29);
        # ``analyzer_version`` reads the module-level ``RULE_VERSION``
        # constant with a literal ``"v1"`` fallback for forward-compat;
        # ``tree_sitter_version`` reads the library's ``__version__`` with
        # ``"unknown"`` fallback (modern tree_sitter builds omit it).
        # ``changeset_paths`` / ``changeset_watcher_confidence`` come
        # verbatim from the detector so replay can rehydrate the exact same
        # ChangeSet (AC #40).
        extra = scan_started_payload.get("extra")
        if not isinstance(extra, dict):
            extra = {}
            scan_started_payload["extra"] = extra
        extra["policy_sha256"] = _compute_policy_sha256(root)
        extra["analyzer_version"] = getattr(
            _unused_import, "RULE_VERSION", "v1"
        )
        extra["tree_sitter_version"] = getattr(
            tree_sitter, "__version__", "unknown"
        )
        extra["changeset_paths"] = list(changeset.paths)
        extra["changeset_watcher_confidence"] = changeset.watcher_confidence

        # Capture the ``event_id`` written for the ScanStarted row so we
        # can stamp it into ``current_event_id()`` for the remainder of
        # the run. A ``None`` return means the disk write hit OSError; in
        # that case we skip the setter and ``current_event_id()`` remains
        # unset for this run (downstream events log empty-string).
        event_id = _safe_append_with_id(
            root, "ScanStarted", scan_started_payload
        )
        if event_id:
            stack.enter_context(set_event_id(event_id))

        path_count = len(changeset.paths)
        _progress(
            f"{path_count} candidate path{'' if path_count == 1 else 's'} "
            f"(mode={watcher_confidence})"
        )

        # --- 3. Funnel -----------------------------------------------------------
        try:
            result = run_scan(root, changeset, scan_id, progress=_progress, analyzer_set=analyzer_set)
        except Exception as exc:
            traceback_str = traceback.format_exc()
            print(
                f"autofix: scan failed: {exc}\n{traceback_str}",
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
            root / ".autofix" / "scans" / scan_id / "findings.sarif"
        )
        # SEC-01 defense-in-depth: the scan_id validator above blocks ``..`` and
        # absolute paths, but verify containment once more after path composition
        # so a future refactor cannot silently reintroduce escape.
        try:
            sarif_path.resolve().relative_to(root.resolve())
        except ValueError:
            print(
                f"autofix: refused to write SARIF outside {root}: {sarif_path}",
                file=sys.stderr,
            )
            return 1
        _progress(f"Writing SARIF to {sarif_path}...")
        try:
            emit_sarif(scan_id, result.findings, sarif_path)
        except Exception as exc:
            print(
                f"autofix: SARIF emit failed: {exc}",
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

        _print_summary(result.findings, sarif_path)
        return 0


def _print_summary(findings: list, sarif_path: Path) -> None:
    """Print a human-readable scan summary to stderr.

    Lists each finding (capped at 20) followed by the SARIF path. Goes
    to stderr so scripts that pipe `autofix scan` into something else
    keep getting clean output channels.
    """
    if findings:
        for finding in findings[:20]:
            path = getattr(finding, "path", "?")
            line = getattr(finding, "start_line", "?")
            rule = getattr(finding, "rule_id", "?")
            symbol = getattr(finding, "symbol_name", "")
            print(
                f"{path}:{line}  warning  {rule}  {symbol}".rstrip(),
                file=sys.stderr,
            )
        if len(findings) > 20:
            print(
                f"... and {len(findings) - 20} more "
                "(see SARIF for the full list)",
                file=sys.stderr,
            )
    summary = (
        f"{len(findings)} finding"
        f"{'' if len(findings) == 1 else 's'} "
        f"written to {sarif_path}"
    )
    print(summary, file=sys.stderr)


__all__ = ["HELP_DESCRIPTION", "HELP_EPILOG", "add_arguments", "run"]
