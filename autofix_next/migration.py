"""Legacy → next state migration helpers.

This module exposes a single public function, :func:`load_legacy_findings`,
which reads the legacy ``.autofix/state/current/findings.json`` (via
:func:`autofix.state.load_findings`) and projects the surviving entries
into :class:`autofix_next.evidence.schema.CandidateFinding` records.

Design constraints (per spec ``state-migration-legacy-to-next``):

* The on-disk read path is **delegated** to ``autofix.state.load_findings``;
  this module never re-implements JSON parsing, path resolution, or the
  ``_migrate_legacy_state_layout`` step.
* Entries with ``status`` in ``{"fixed", "issue-opened"}`` are filtered
  out before projection. All other status values (``"new"``, ``"failed"``,
  unrecognised strings, missing) are retained.
* Entries missing ``finding_id`` are skipped with a single log emission
  per skipped entry; status-filter rejects do not emit any log.
* The function is read-only: it performs zero writes to
  ``.autofix/state/**``, ``.autofix/autofix-policy.json``, or
  ``.autofix/events.jsonl``.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import autofix.state as _legacy_state
from autofix_next.evidence.schema import CandidateFinding

__all__ = ["load_legacy_findings"]

# Statuses whose findings are considered terminal in the legacy ledger and
# must not appear in the next-pipeline candidate set.
_DROPPED_STATUSES: frozenset[str] = frozenset({"fixed", "issue-opened"})

# task-20260506-001 sec-002 (audit, security-auditor): rule_id flows from
# the legacy ``category`` into ``triage.resolve_template`` which constructs
# a filesystem path under ``llm/prompts/``. The prevention rule from
# task-20260417-008 SEC-002 requires rule_id values to match this regex
# before they cross that boundary.
_RULE_ID_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _is_safe_relpath(value: str) -> bool:
    """Return True iff ``value`` is a safe relative path under the repo root.

    task-20260506-001 sec-001 (audit, security-auditor): legacy
    ``evidence['file']`` values are untrusted and a malicious entry could
    set ``"../../../etc/passwd"``. Reject:

    * absolute paths (POSIX or Windows-rooted)
    * any segment equal to ``..`` (parent escape)
    * embedded NUL bytes

    An empty string is considered safe (the projection emits ``""`` for
    missing files; the dedup cascade and SARIF emitter both tolerate it).
    """
    if value == "":
        return True
    if "\x00" in value:
        return False
    if value.startswith(("/", "\\")) or (len(value) >= 2 and value[1] == ":"):
        return False
    parts = PurePosixPath(value.replace("\\", "/")).parts
    return ".." not in parts


def _coerce_line(raw: Any) -> int:
    """Best-effort coercion of an evidence ``line`` value to a non-negative int.

    Returns 0 for missing keys (``None``), non-numeric strings, and any
    other type that ``int(...)`` cannot accept. The legacy schema does not
    guarantee ``line`` is an int, so every projection must tolerate junk.
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _project(legacy: dict[str, Any]) -> CandidateFinding:
    """Project a single legacy finding dict into a :class:`CandidateFinding`.

    The caller has already verified the entry passed the status filter and
    carries a ``finding_id``. All evidence-derived fields fall back to
    documented defaults rather than raising on missing/null keys.
    """
    evidence = legacy.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}

    description = legacy.get("description") or ""
    if not isinstance(description, str):
        description = ""

    raw_file = evidence.get("file", "")
    path_value = raw_file if isinstance(raw_file, str) else ""

    raw_symbol = evidence.get("symbol")
    if isinstance(raw_symbol, str) and raw_symbol:
        symbol_name = raw_symbol
    else:
        symbol_name = description[:64]

    line_int = _coerce_line(evidence.get("line"))

    # changed_slice uses the *raw* evidence line value so a bogus int-like
    # string round-trips faithfully into the slice string. The slice is
    # only emitted when ``file`` is non-empty.
    if path_value:
        slice_symbol = evidence.get("symbol", "")
        if not isinstance(slice_symbol, str):
            slice_symbol = ""
        slice_line = evidence.get("line", "")
        changed_slice = f"{path_value}:{slice_line}@{slice_symbol}"
    else:
        changed_slice = ""

    rule_id_raw = legacy.get("category", "")
    rule_id = rule_id_raw if isinstance(rule_id_raw, str) else ""

    confidence_raw = legacy.get("confidence_score", 1.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 1.0
    analyzer_confidence = max(0.0, min(1.0, confidence))

    return CandidateFinding(
        rule_id=rule_id,
        path=path_value,
        symbol_name=symbol_name,
        normalized_import="",
        start_line=line_int,
        end_line=line_int,
        changed_slice=changed_slice,
        finding_id=legacy["finding_id"],
        analyzer_confidence=analyzer_confidence,
    )


def load_legacy_findings(
    root: Path,
    *,
    log: Callable[[str], None] | None = None,
) -> list[CandidateFinding]:
    """Load the legacy ``findings.json`` and return a list of CandidateFindings.

    Behaviour (binding contract; see spec ACs 2-8):

    * Delegates the on-disk read to :func:`autofix.state.load_findings`,
      forwarding the ``log`` callable so corrupt-JSON warnings are emitted
      with the substring ``"could not load findings file"``.
    * Returns an empty list when the legacy file is absent or malformed —
      never raises.
    * Drops entries whose ``status`` is ``"fixed"`` or ``"issue-opened"``
      *before* finding_id validation, so terminal entries do not produce
      ``"missing finding_id"`` log noise.
    * Skips entries missing the ``finding_id`` key and, when ``log`` is
      provided, emits exactly one message per skipped entry containing
      the substring ``"missing finding_id"``.
    * Performs zero writes to any locked surface; the only IO is the
      single delegated read.

    Parameters
    ----------
    root:
        The repository root used to locate ``.autofix/state/current/findings.json``.
    log:
        Optional sink for diagnostic strings. ``None`` silences all logs,
        including the corrupt-JSON warning emitted by the delegated read.
    """
    # Delegate read + path resolution + JSON parse to the legacy loader.
    # autofix.state.load_findings already handles the missing-file and
    # malformed-JSON branches per its own contract (returning [] and
    # emitting a 'could not load findings file' warning when log is set).
    raw_entries = _legacy_state.load_findings(root, log=log)

    if not isinstance(raw_entries, list):
        # Defensive: load_findings's contract returns list[dict], but if
        # the underlying file holds an unexpected top-level shape the
        # legacy helper may return []; treat anything non-list as empty.
        return []

    projected: list[CandidateFinding] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            # Skip stray non-dict items silently — the legacy schema
            # never produced these, so logging would be noise.
            continue

        status = entry.get("status")
        if isinstance(status, str) and status in _DROPPED_STATUSES:
            # AC 5: drop terminal-status entries before any other check.
            continue

        if "finding_id" not in entry:
            # AC 6: skip and log once per offending entry.
            if log is not None:
                log("Skipping legacy finding: missing finding_id")
            continue

        # task-20260506-001 sec-001 / sec-002 (audit): validate untrusted
        # category and evidence['file'] BEFORE projection so traversal
        # sequences and unknown rule_id shapes never reach the dedup
        # cascade or triage.resolve_template path constructor.
        finding_id = entry.get("finding_id", "")
        category = entry.get("category", "")
        if not isinstance(category, str) or not _RULE_ID_RE.match(category):
            if log is not None:
                log(
                    f"Skipping legacy finding {finding_id!r}: "
                    "category does not match the rule_id allowlist"
                )
            continue

        evidence = entry.get("evidence")
        if isinstance(evidence, dict):
            raw_file = evidence.get("file", "")
            if isinstance(raw_file, str) and not _is_safe_relpath(raw_file):
                if log is not None:
                    log(
                        f"Skipping legacy finding {finding_id!r}: "
                        "evidence['file'] is not a safe repo-relative path"
                    )
                continue

        projected.append(_project(entry))

    return projected
