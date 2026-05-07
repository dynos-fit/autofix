"""Mypy linter passthrough adapter.

Invokes the mypy binary as a subprocess and converts its text output into
:class:`CandidateFinding` records. Handles missing binary, timeouts, and
subprocess errors gracefully.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from autofix.evidence.schema import CandidateFinding
from autofix.indexing.symbols import SymbolTable
from autofix.parsing.tree_sitter import ParseResult
from autofix.telemetry.correlation import current_scan_id
from autofix.telemetry import events_log

RULE_ID_PREFIX: str = "linter:mypy"
RULE_VERSION: str = "v1"
_TIMEOUT_SECONDS: float = 60.0

# Per-scan memoization: maps scan_id -> set of event types already logged.
# This ensures we log "AnalyzerUnavailable" and "AnalyzerTimeout" at most once per scan.
_PER_SCAN_EVENTS: dict[str, set[str]] = {}

# Compiled regex to parse mypy output lines.
_LINE_RE = re.compile(
    r'^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>error|note|warning):\s*(?P<msg>.+?)(?:\s+\[(?P<code>[^\]]+)\])?\s*$'
)


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
    """Invoke mypy linter and convert text output to candidate findings.

    Runs ``mypy --no-error-summary --no-color-output --show-column-numbers
    --no-incremental -- <file>`` on the file at ``parse_result.relpath``.
    On success, parses the text output and yields :class:`CandidateFinding`
    records for errors only (skipping notes and warnings).

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
        Zero or more findings from mypy errors, one per linter message. Each
        finding includes the mypy error code and location.
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
            [
                "mypy",
                "--no-error-summary",
                "--no-color-output",
                "--show-column-numbers",
                "--no-incremental",
                "--",
                str(abs_path),
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        # mypy binary is not installed.
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
        # mypy took too long (60s timeout).
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

    # Parse text output line by line.
    if not proc.stdout.strip():
        return iter([])

    findings: list[CandidateFinding] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue

        m = _LINE_RE.match(line)
        if not m:
            # Skip lines that don't match the expected format.
            continue

        sev = m.group("sev")
        # Skip notes and warnings; emit findings only for errors.
        if sev != "error":
            continue

        try:
            error_code = m.group("code") or "unknown"
            rule_id = f"{RULE_ID_PREFIX}:{error_code}"
            start_line = int(m.group("line"))
            end_line = start_line
            message = m.group("msg").rstrip()

            finding = CandidateFinding(
                rule_id=rule_id,
                path=parse_result.relpath,
                symbol_name=error_code,
                normalized_import="",
                start_line=start_line,
                end_line=end_line,
                changed_slice=message,
                finding_id="",  # Not computed for linter passthrough
                provenance=rule_id,
            )
            findings.append(finding)
        except (ValueError, AttributeError):
            # Skip malformed items.
            continue

    return iter(findings)


__all__ = ["RULE_ID_PREFIX", "RULE_VERSION", "analyze"]
