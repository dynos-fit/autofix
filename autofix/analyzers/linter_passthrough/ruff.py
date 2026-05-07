"""Ruff linter passthrough adapter.

Invokes the ruff binary as a subprocess and converts its JSON output into
:class:`CandidateFinding` records. Handles missing binary, timeouts, and
subprocess errors gracefully.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from pathlib import Path

from autofix.evidence.schema import CandidateFinding
from autofix.indexing.symbols import SymbolTable
from autofix.parsing.tree_sitter import ParseResult
from autofix.telemetry.correlation import current_scan_id
from autofix.telemetry import events_log

RULE_ID_PREFIX: str = "linter:ruff"
RULE_VERSION: str = "v1"

# Per-scan memoization: maps scan_id -> set of event types already logged.
# This ensures we log "AnalyzerUnavailable" and "AnalyzerTimeout" at most once per scan.
_PER_SCAN_EVENTS: dict[str, set[str]] = {}


def _reset_per_scan_state() -> None:
    """Clear the per-scan memoization state.

    Called by tests and scan runners to reset state between scans.
    Not part of the public API.
    """
    _PER_SCAN_EVENTS.clear()


def _should_log_event(scan_id: str, event_type: str) -> bool:
    """Return True if we should log this event for this scan (first time only).

    Updates the internal memoization set.
    """
    if scan_id not in _PER_SCAN_EVENTS:
        _PER_SCAN_EVENTS[scan_id] = set()
    if event_type in _PER_SCAN_EVENTS[scan_id]:
        return False
    _PER_SCAN_EVENTS[scan_id].add(event_type)
    return True


def analyze(
    parse_result: ParseResult, symbol_table: SymbolTable
) -> Iterable[CandidateFinding]:
    """Invoke ruff linter and convert JSON output to candidate findings.

    Runs ``ruff check --output-format=json --no-cache`` on the file at
    ``parse_result.relpath``. On success, parses the JSON output and yields
    :class:`CandidateFinding` records.

    On error conditions (missing binary, timeout, subprocess failure), logs
    a telemetry event (once per scan) and returns an empty iterable. Never
    raises an exception.

    Parameters
    ----------
    parse_result:
        Output of :func:`autofix.parsing.tree_sitter.parse_file`.
    symbol_table:
        Output of :func:`autofix.indexing.symbols.build_symbol_table`
        (unused, required by analyzer interface).

    Yields
    ------
    CandidateFinding
        Zero or more findings from ruff, one per linter message. Each
        finding includes the ruff rule code and location.
    """

    # Absolute path is parse_result.path (already computed by parse_file).
    # Repo root is derived by removing the relative path components from
    # the absolute path.
    abs_path = parse_result.path
    relpath_parts = Path(parse_result.relpath).parts
    root = abs_path
    for _ in relpath_parts:
        root = root.parent

    scan_id = current_scan_id() or "_no_scan"

    try:
        proc = subprocess.run(
            ["ruff", "check", "--output-format=json", "--no-cache", "--", str(abs_path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        # ruff binary is not installed.
        if _should_log_event(scan_id, "AnalyzerUnavailable"):
            try:
                events_log.append_event(
                    root,
                    "AnalyzerUnavailable",
                    {"analyzer": RULE_ID_PREFIX, "scan_id": scan_id},
                )
            except OSError:
                pass
        return iter([])
    except subprocess.TimeoutExpired:
        # ruff took too long (30s timeout).
        if _should_log_event(scan_id, "AnalyzerTimeout"):
            try:
                events_log.append_event(
                    root,
                    "AnalyzerTimeout",
                    {
                        "analyzer": RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "file": str(parse_result.relpath),
                    },
                )
            except OSError:
                pass
        return iter([])

    # Check for subprocess errors (non-zero exit with no stdout).
    if proc.returncode != 0 and not proc.stdout:
        stderr_msg = proc.stderr[:1024] if proc.stderr else ""
        if _should_log_event(scan_id, "AnalyzerError"):
            try:
                events_log.append_event(
                    root,
                    "AnalyzerError",
                    {
                        "analyzer": RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "file": str(parse_result.relpath),
                        "stderr": stderr_msg,
                    },
                )
            except OSError:
                pass
        return iter([])

    # Parse JSON output.
    try:
        if not proc.stdout.strip():
            return iter([])
        items = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        # Malformed JSON from ruff.
        if _should_log_event(scan_id, "AnalyzerError"):
            try:
                events_log.append_event(
                    root,
                    "AnalyzerError",
                    {
                        "analyzer": RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "file": str(parse_result.relpath),
                        "stderr": "Failed to parse JSON output",
                    },
                )
            except OSError:
                pass
        return iter([])

    # Audit SEC-RUFF-01: ruff's documented schema is a JSON array of
    # objects. A future version, a misconfiguration, or a hostile fixture
    # could return a non-list root or list-of-non-dicts. Iterating those
    # raises AttributeError on `.get()` calls below. Reject non-conforming
    # shapes loud-and-empty.
    if not isinstance(items, list):
        if _should_log_event(scan_id, "AnalyzerError"):
            try:
                events_log.append_event(
                    root,
                    "AnalyzerError",
                    {
                        "analyzer": RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "file": str(parse_result.relpath),
                        "stderr": (
                            f"ruff JSON root is {type(items).__name__}, "
                            "expected list"
                        ),
                    },
                )
            except OSError:
                pass
        return iter([])

    # Convert ruff items to CandidateFinding records.
    findings: list[CandidateFinding] = []
    for item in items:
        if not isinstance(item, dict):
            # Skip stray non-dict entries; do not raise.
            continue
        try:
            rule_code = item.get("code", "unknown")
            rule_id = f"{RULE_ID_PREFIX}:{rule_code}"
            location = item.get("location", {})
            end_location = item.get("end_location", {})
            start_line = location.get("row", 0)
            end_line = end_location.get("row", start_line)
            message = item.get("message", "")

            finding = CandidateFinding(
                rule_id=rule_id,
                path=parse_result.relpath,
                symbol_name=rule_code,
                normalized_import="",
                start_line=start_line,
                end_line=end_line,
                changed_slice=message,
                finding_id="",  # Not computed for linter passthrough
                provenance=rule_id,
            )
            findings.append(finding)
        except (KeyError, ValueError):
            # Skip malformed items.
            continue

    return iter(findings)


__all__ = ["RULE_ID_PREFIX", "RULE_VERSION", "analyze"]
