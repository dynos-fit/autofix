"""ESLint linter passthrough adapter.

Invokes the eslint binary as a subprocess and converts its JSON output into
:class:`CandidateFinding` records. Handles missing binary, missing project
markers, timeouts, and subprocess errors gracefully. Only severity==2
(error) messages are emitted; severity==1 (warning) and severity==0 (off)
are dropped.
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

RULE_ID_PREFIX: str = "linter:eslint"
RULE_VERSION: str = "v1"

_TIMEOUT_SECONDS: float = 30

# eslint config filenames that mark a project as eslint-enabled.
_ESLINT_CONFIG_NAMES: tuple[str, ...] = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
)

# Per-scan memoization: maps scan_id -> set of event types already logged.
# This ensures we log each event type at most once per scan.
_PER_SCAN_EVENTS: dict[str, set[str]] = {}

# Per-scan project-marker and binary availability cache.
# Outer key: scan_id. Inner dict keys: "package_json", "eslint_config", "binary".
# True = available; False = missing / should short-circuit.
_PER_SCAN_MARKERS: dict[str, dict[str, bool]] = {}


def _reset_per_scan_state() -> None:
    """Clear the per-scan memoization state.

    Called by tests and scan runners to reset state between scans.
    Not part of the public API.
    """
    _PER_SCAN_EVENTS.clear()
    _PER_SCAN_MARKERS.clear()


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


def _check_markers(root: Path, scan_id: str) -> dict[str, bool]:
    """Check and memoize project markers for the given scan.

    Returns the inner marker dict for the scan. After any False value,
    no further filesystem walk is performed on subsequent calls.
    """
    if scan_id in _PER_SCAN_MARKERS:
        return _PER_SCAN_MARKERS[scan_id]

    markers: dict[str, bool] = {}

    # Check package.json
    markers["package_json"] = (root / "package.json").is_file()
    if not markers["package_json"]:
        # Short-circuit: mark remaining keys False without filesystem walks.
        markers["eslint_config"] = False
        markers["binary"] = False
        _PER_SCAN_MARKERS[scan_id] = markers
        return markers

    # Check for at least one eslint config file
    markers["eslint_config"] = any(
        (root / name).is_file() for name in _ESLINT_CONFIG_NAMES
    )
    if not markers["eslint_config"]:
        markers["binary"] = False
        _PER_SCAN_MARKERS[scan_id] = markers
        return markers

    # Binary check deferred to first subprocess call; default True until proven False
    markers["binary"] = True
    _PER_SCAN_MARKERS[scan_id] = markers
    return markers


def analyze(
    parse_result: ParseResult, symbol_table: SymbolTable
) -> Iterable[CandidateFinding]:
    """Invoke eslint linter and convert JSON output to candidate findings.

    Runs ``eslint --format json -- <file>`` on the file at
    ``parse_result.relpath``. On success, parses the JSON output and yields
    :class:`CandidateFinding` records for severity==2 (error) messages only.

    Before invoking the binary, the adapter checks for project markers
    (``package.json`` and an eslint config file) at the repository root.
    If markers are missing or the binary is absent, the adapter returns an
    empty iterable and logs one ``AnalyzerSkipped`` event per scan.

    On other error conditions (timeout, subprocess failure), logs a telemetry
    event (once per scan) and returns an empty iterable. Never raises an
    exception.

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
        Zero or more findings from eslint errors, one per severity==2 message.
        Each finding includes the eslint rule ID and location.
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

    # --- Marker probe (memoized per scan_id) ---
    markers = _check_markers(root, scan_id)

    if not markers["package_json"]:
        if _should_log_event(scan_id, "no_package_json"):
            try:
                events_log.append_event(
                    root,
                    "AnalyzerSkipped",
                    {
                        "analyzer": RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "reason": "no_package_json",
                    },
                )
            except OSError:
                pass
        return iter([])

    if not markers["eslint_config"]:
        if _should_log_event(scan_id, "no_eslint_config"):
            try:
                events_log.append_event(
                    root,
                    "AnalyzerSkipped",
                    {
                        "analyzer": RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "reason": "no_eslint_config",
                    },
                )
            except OSError:
                pass
        return iter([])

    # Short-circuit if binary was already found to be absent in this scan.
    if not markers["binary"]:
        if _should_log_event(scan_id, "no_eslint_binary"):
            try:
                events_log.append_event(
                    root,
                    "AnalyzerSkipped",
                    {
                        "analyzer": RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "reason": "no_eslint_binary",
                    },
                )
            except OSError:
                pass
        return iter([])

    # --- Subprocess invocation ---
    try:
        proc = subprocess.run(
            ["eslint", "--format", "json", "--", str(abs_path)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        # eslint binary is not installed. Memoize so subsequent files skip.
        _PER_SCAN_MARKERS[scan_id]["binary"] = False
        if _should_log_event(scan_id, "no_eslint_binary"):
            try:
                events_log.append_event(
                    root,
                    "AnalyzerSkipped",
                    {
                        "analyzer": RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "reason": "no_eslint_binary",
                    },
                )
            except OSError:
                pass
        return iter([])
    except subprocess.TimeoutExpired:
        # eslint took too long.
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
    # NOTE: eslint exits 1 when it finds problems but still populates stdout.
    # Only trip the crash branch when stdout is empty (genuine failure).
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

    # --- Parse JSON output ---
    try:
        if not proc.stdout.strip():
            return iter([])
        file_objects = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        # Malformed JSON from eslint.
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

    # eslint's JSON root must be a list of file objects.
    if not isinstance(file_objects, list):
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
                            f"eslint JSON root is {type(file_objects).__name__}, "
                            "expected list"
                        ),
                    },
                )
            except OSError:
                pass
        return iter([])

    # --- Convert eslint items to CandidateFinding records ---
    findings: list[CandidateFinding] = []
    for file_obj in file_objects:
        if not isinstance(file_obj, dict):
            # Skip stray non-dict entries.
            continue
        messages = file_obj.get("messages")
        if not isinstance(messages, list):
            continue
        for item in messages:
            if not isinstance(item, dict):
                continue
            # Severity filter: only emit severity==2 (error); drop 0=off, 1=warning.
            try:
                severity = item.get("severity")
                if severity != 2:
                    continue

                rule_code = item.get("ruleId") or "unknown"
                rule_id = f"{RULE_ID_PREFIX}:{rule_code}"
                start_line = int(item.get("line", 0))
                end_line = int(item.get("endLine", start_line))
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
            except (KeyError, ValueError, TypeError):
                # Skip malformed message items.
                continue

    return iter(findings)


__all__ = ["RULE_ID_PREFIX", "RULE_VERSION", "analyze"]
