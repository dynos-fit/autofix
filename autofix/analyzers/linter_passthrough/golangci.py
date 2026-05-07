"""golangci-lint passthrough adapter.

Invokes the golangci-lint binary as a subprocess and converts its JSON output
into :class:`CandidateFinding` records. Handles missing binary, missing go.mod
marker, v1/v2 flag-form differences, timeouts, and subprocess errors gracefully.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from autofix.evidence.schema import CandidateFinding
from autofix.indexing.symbols import SymbolTable
from autofix.parsing.tree_sitter import ParseResult
from autofix.telemetry.correlation import current_scan_id
from autofix.telemetry import events_log

RULE_ID_PREFIX: str = "linter:golangci"
RULE_VERSION: str = "v1"
_TIMEOUT_SECONDS: float = 60.0

# Per-scan memoization: maps scan_id -> set of event types already logged.
# This ensures we log each event type at most once per scan.
_PER_SCAN_EVENTS: dict[str, set[str]] = {}

# Per-scan marker and state cache.
# Inner dict keys: "go_mod" (bool), "binary" (bool), "flag_form" (str|None).
_PER_SCAN_MARKERS: dict[str, dict[str, Any]] = {}

# Per-scan flag-form memoization: scan_id -> "v1" | "v2"
_PER_SCAN_FLAG_FORM: dict[str, str] = {}


def _reset_per_scan_state() -> None:
    """Clear the per-scan memoization state.

    Called by tests and scan runners to reset state between scans.
    Not part of the public API.
    """
    _PER_SCAN_EVENTS.clear()
    _PER_SCAN_MARKERS.clear()
    _PER_SCAN_FLAG_FORM.clear()


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


def _run_golangci(abs_path: Path, flag_form: str) -> subprocess.CompletedProcess[str]:
    """Run golangci-lint with the given flag form."""
    if flag_form == "v2":
        flag = "--output.formats=json"
    else:
        flag = "--out-format=json"
    return subprocess.run(
        ["golangci-lint", "run", flag, str(abs_path)],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


def analyze(
    parse_result: ParseResult, symbol_table: SymbolTable
) -> Iterable[CandidateFinding]:
    """Invoke golangci-lint and convert JSON output to candidate findings.

    Runs ``golangci-lint run --output.formats=json <abs_path>`` (v2 form) on
    the first file of a scan. If golangci-lint reports an unknown flag or
    unknown command, retries with ``--out-format=json`` (v1 form) and memoizes
    the chosen flag form for the remainder of the scan.

    On error conditions (missing binary, missing go.mod, timeout, subprocess
    failure), logs a telemetry event (once per scan) and returns an empty
    iterable. Never raises an exception.

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
        Zero or more findings from golangci-lint, one per issue. Each
        finding includes the linter name and location.
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

    # Initialize per-scan marker dict if needed.
    if scan_id not in _PER_SCAN_MARKERS:
        _PER_SCAN_MARKERS[scan_id] = {"go_mod": None, "binary": None, "flag_form": None}

    markers = _PER_SCAN_MARKERS[scan_id]

    # Check go.mod marker (memoized per scan_id).
    if markers["go_mod"] is None:
        markers["go_mod"] = (root / "go.mod").is_file()

    if not markers["go_mod"]:
        if _should_log_event(scan_id, "no_go_mod"):
            try:
                events_log.append_event(
                    root,
                    "AnalyzerSkipped",
                    {
                        "analyzer": RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "reason": "no_go_mod",
                    },
                )
            except OSError:
                pass
        return iter([])

    # Check binary availability (memoized per scan_id).
    if markers["binary"] is None:
        markers["binary"] = shutil.which("golangci-lint") is not None

    if not markers["binary"]:
        if _should_log_event(scan_id, "no_golangci_binary"):
            try:
                events_log.append_event(
                    root,
                    "AnalyzerSkipped",
                    {
                        "analyzer": RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "reason": "no_golangci_binary",
                    },
                )
            except OSError:
                pass
        return iter([])

    # Determine flag form (v2 or v1), memoized per scan_id.
    known_flag_form = _PER_SCAN_FLAG_FORM.get(scan_id)

    if known_flag_form is not None:
        # Already probed: use the memoized form directly.
        try:
            proc = _run_golangci(abs_path, known_flag_form)
        except subprocess.TimeoutExpired:
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
        except FileNotFoundError:
            # Binary disappeared between the check and the run.
            if _should_log_event(scan_id, "no_golangci_binary"):
                try:
                    events_log.append_event(
                        root,
                        "AnalyzerSkipped",
                        {
                            "analyzer": RULE_ID_PREFIX,
                            "scan_id": scan_id,
                            "reason": "no_golangci_binary",
                        },
                    )
                except OSError:
                    pass
            return iter([])
    else:
        # First file of this scan: probe v2 flag form.
        try:
            proc = _run_golangci(abs_path, "v2")
        except subprocess.TimeoutExpired:
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
        except FileNotFoundError:
            # Binary disappeared between the check and the run.
            if _should_log_event(scan_id, "no_golangci_binary"):
                try:
                    events_log.append_event(
                        root,
                        "AnalyzerSkipped",
                        {
                            "analyzer": RULE_ID_PREFIX,
                            "scan_id": scan_id,
                            "reason": "no_golangci_binary",
                        },
                    )
                except OSError:
                    pass
            return iter([])

        # Check if the v2 flag was rejected (unknown flag or unknown command).
        stderr_text = proc.stderr or ""
        if proc.returncode != 0 and (
            "unknown flag" in stderr_text or "unknown command" in stderr_text
        ):
            # Retry with v1 flag form.
            try:
                proc = _run_golangci(abs_path, "v1")
            except subprocess.TimeoutExpired:
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
            except FileNotFoundError:
                if _should_log_event(scan_id, "no_golangci_binary"):
                    try:
                        events_log.append_event(
                            root,
                            "AnalyzerSkipped",
                            {
                                "analyzer": RULE_ID_PREFIX,
                                "scan_id": scan_id,
                                "reason": "no_golangci_binary",
                            },
                        )
                    except OSError:
                        pass
                return iter([])
            # Memoize v1 for this scan.
            _PER_SCAN_FLAG_FORM[scan_id] = "v1"
        else:
            # v2 worked (or failed for a different reason); memoize v2.
            _PER_SCAN_FLAG_FORM[scan_id] = "v2"

    # Check for subprocess errors (non-zero exit with no stdout).
    # EXCEPTION: this check runs AFTER the v1/v2 flag fallback has been settled.
    if proc.returncode != 0 and not proc.stdout:
        stderr_msg = (proc.stderr or "")[:1024]
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
    if not proc.stdout.strip():
        return iter([])

    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
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

    # Validate JSON shape: must be a dict with an "Issues" list.
    if not isinstance(data, dict):
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
                            f"golangci-lint JSON root is {type(data).__name__}, "
                            "expected dict"
                        ),
                    },
                )
            except OSError:
                pass
        return iter([])

    issues = data.get("Issues")
    if not isinstance(issues, list):
        if _should_log_event(scan_id, "AnalyzerError"):
            try:
                issues_type = type(issues).__name__ if issues is not None else "null"
                events_log.append_event(
                    root,
                    "AnalyzerError",
                    {
                        "analyzer": RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "file": str(parse_result.relpath),
                        "stderr": (
                            f"golangci-lint JSON Issues field is {issues_type}, "
                            "expected list"
                        ),
                    },
                )
            except OSError:
                pass
        return iter([])

    # Convert issues to CandidateFinding records.
    findings: list[CandidateFinding] = []
    for item in issues:
        if not isinstance(item, dict):
            # Skip stray non-dict entries; do not raise.
            continue
        try:
            linter = item.get("FromLinter") or "unknown"
            rule_id = f"{RULE_ID_PREFIX}:{linter}"
            pos = item.get("Pos", {}) or {}
            start_line = int(pos.get("Line", 0))
            end_line = start_line  # golangci has no end-line field
            message = item.get("Text", "")

            finding = CandidateFinding(
                rule_id=rule_id,
                path=parse_result.relpath,
                symbol_name=linter,
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
