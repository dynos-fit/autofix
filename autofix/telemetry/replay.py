"""Replay engine — deterministic pre-LLM re-execution of a past scan.

Given a ``scan_id`` and the repository ``root``, the replay engine parses
``.autofix/events.jsonl`` for the matching ``ScanStarted`` anchor, reconstructs
the :class:`ChangeSet` from the anchor's ``extra``, re-runs the funnel pipeline
inside :func:`replay_mode` (no git checkout, no LLM call), and compares the
reproduced :class:`CandidateFinding` set against the historical findings
recovered from subsequent events (``CandidateFindingProduced`` /
``EvidencePacketBuilt`` / ``LLMCallGated`` / ``PriorityScored``).

The returned :class:`ScanRun` classifies the outcome as one of four verdicts —
``"match"``, ``"mismatch"``, ``"missing_anchor"``, ``"version_drift"`` — and
carries the per-finding pairing + prompt-prefix-hash recomputation results so
downstream tooling can diagnose non-determinism without replaying a second
time.

Spec ACs: 31-40, 44, 45.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from autofix.evidence.schema import CandidateFinding

# -- Module-level context vars (AC 38) --------------------------------------

_REPLAY_ACTIVE: ContextVar[bool] = ContextVar("_REPLAY_ACTIVE", default=False)
_REPLAY_EVENTS_SINK: ContextVar[list | None] = ContextVar(
    "_REPLAY_EVENTS_SINK", default=None
)


# -- Dataclasses (ACs 32, 33) -----------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalFinding:
    """A finding reconstructed from the historical events.jsonl stream.

    Five fields, frozen + slots — the shape is part of the replay contract
    and must match AC 33 exactly.
    """

    rule_id: str
    primary_symbol: str
    finding_id: str
    prompt_prefix_hash: str
    priority: float


@dataclass(frozen=True, slots=True)
class ScanRun:
    """The outcome of one :func:`replay` invocation.

    Field order is pinned by AC 32 and must not be reordered — the
    replay-reporting surface depends on it. ``drift_reason`` is only
    populated when ``verdict == "version_drift"``; it is ``None`` on every
    other verdict (AC 35 explicitly requires ``None`` on missing_anchor).
    """

    scan_id: str
    commit_sha: str
    historical_findings: tuple[HistoricalFinding, ...]
    reproduced_findings: tuple[CandidateFinding, ...]
    matched: tuple[tuple[str, str], ...]
    historical_only: tuple[str, ...]
    reproduced_only: tuple[str, ...]
    prompt_prefix_hash_matched: tuple[str, ...]
    prompt_prefix_hash_mismatched: tuple[tuple[str, str, str], ...]
    verdict: Literal["match", "mismatch", "missing_anchor", "version_drift"]
    drift_reason: str | None = None


# -- Replay-mode context manager (AC 39) ------------------------------------


@contextlib.contextmanager
def replay_mode() -> Iterator[None]:
    """Context manager that marks the current thread/task as in replay mode.

    Entering sets ``_REPLAY_ACTIVE=True`` and ``_REPLAY_EVENTS_SINK=[]``;
    exiting resets both via the tokens returned by ``ContextVar.set``. The
    pipeline's telemetry sinks check ``_REPLAY_ACTIVE`` before writing to
    ``events.jsonl`` so a replay re-run does not corrupt the historical
    stream.
    """
    active_token = _REPLAY_ACTIVE.set(True)
    sink_token = _REPLAY_EVENTS_SINK.set([])
    try:
        yield
    finally:
        # Reset in reverse order so the stack unwinds symmetrically.
        _REPLAY_EVENTS_SINK.reset(sink_token)
        _REPLAY_ACTIVE.reset(active_token)


# -- replay() entry point ---------------------------------------------------


def replay(
    scan_id: str,
    root: Path,
    *,
    run_scan_fn: Any = None,
) -> ScanRun:
    """Re-execute the scan identified by ``scan_id`` deterministically.

    Parameters
    ----------
    scan_id:
        The opaque identifier that was threaded into every events.jsonl row
        emitted by the original scan. Must be a non-empty string.
    root:
        Repository root. Must contain ``.autofix/events.jsonl``; other
        artefacts (policy file, cluster store) are read best-effort.
    run_scan_fn:
        Optional callable matching ``autofix.funnel.pipeline.run_scan``'s
        signature. PROACTIVE-09: telemetry conceptually sits below funnel
        in the dependency stack, but ``replay`` needs to call into the
        funnel to re-execute a scan. Without this kwarg, ``replay`` does
        a function-body import of ``autofix.funnel.pipeline.run_scan`` —
        the established pattern for breaking the circular dependency.
        Tests and external integrators can inject a mock here to avoid
        triggering the funnel import path entirely.

    Returns
    -------
    ScanRun
        The verdict + per-finding pairing. See the four verdicts pinned by
        AC 34 for the possible outcomes.

    Raises
    ------
    ValueError
        When ``scan_id`` is not a non-empty string (AC 36).
    FileNotFoundError
        When ``.autofix/events.jsonl`` does not exist under ``root`` (AC 36).

    Every other failure mode is mapped to a ``ScanRun`` verdict rather than
    an exception (AC 36). In particular: a missing ``ScanStarted`` anchor
    for the given ``scan_id`` yields ``verdict="missing_anchor"``; a
    mismatch between historical and live version pins yields
    ``verdict="version_drift"`` with ``drift_reason`` set; everything else
    runs the pipeline and yields ``"match"`` or ``"mismatch"``.
    """
    # -- Input validation (AC 36) -------------------------------------------
    if not isinstance(scan_id, str) or not scan_id:
        raise ValueError("scan_id must be a non-empty string")
    root = Path(root)
    events_path = root / ".autofix" / "events.jsonl"
    if not events_path.is_file():
        raise FileNotFoundError(
            f"events.jsonl not found at {events_path}"
        )

    # -- Parse events.jsonl, locate the anchor ------------------------------
    anchor_payload: dict | None = None
    rows_for_scan: list[dict] = []
    try:
        raw_text = events_path.read_text(encoding="utf-8")
    except OSError:
        # Read failure after is_file() succeeded: treat as missing anchor
        # rather than re-raising; the caller already got a positive
        # existence check and we must map non-(ValueError|FileNotFoundError)
        # failures to a verdict per AC 36.
        return _missing_anchor(scan_id)

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # A malformed row cannot anchor or contribute historical
            # findings; skip rather than abort.
            continue
        # events.jsonl rows may be either the payload itself or a wrapper
        # with the payload under "scan_event"/"payload". Probe both shapes.
        if isinstance(row, dict) and isinstance(row.get("scan_event"), dict):
            payload = row["scan_event"]
        elif isinstance(row, dict) and isinstance(row.get("payload"), dict):
            payload = row["payload"]
        else:
            payload = row if isinstance(row, dict) else {}
        if not isinstance(payload, dict):
            continue
        if payload.get("scan_id") != scan_id:
            continue
        event_type = payload.get("event_type") or (
            row.get("event_type") if isinstance(row, dict) else None
        )
        if event_type == "ScanStarted" and anchor_payload is None:
            anchor_payload = payload
        rows_for_scan.append(
            {"event_type": event_type, "payload": payload}
        )

    if anchor_payload is None:
        return _missing_anchor(scan_id)

    extra = anchor_payload.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}
    commit_sha = anchor_payload.get("commit_sha") or ""
    if not isinstance(commit_sha, str):
        commit_sha = ""

    # -- Reconstruct historical findings ------------------------------------
    historical_findings = tuple(_parse_historical_findings(rows_for_scan))

    # -- Version pinning check (AC 37, first-mismatch-wins) -----------------
    historical_policy = str(extra.get("policy_sha256", "") or "")
    historical_analyzer = str(extra.get("analyzer_version", "") or "")
    historical_tree_sitter = str(extra.get("tree_sitter_version", "") or "")
    live_policy = _compute_policy_sha(root)
    live_analyzer = _compute_analyzer_version()
    live_tree_sitter = _compute_tree_sitter_version()

    drift_reason: str | None = None
    if historical_policy != live_policy:
        drift_reason = (
            f"policy_sha256 differs (historical={historical_policy!r}, "
            f"live={live_policy!r})"
        )
    elif historical_analyzer != live_analyzer:
        drift_reason = (
            f"analyzer_version differs (historical={historical_analyzer!r}, "
            f"live={live_analyzer!r})"
        )
    elif historical_tree_sitter != live_tree_sitter:
        drift_reason = (
            f"tree_sitter_version differs "
            f"(historical={historical_tree_sitter!r}, "
            f"live={live_tree_sitter!r})"
        )

    if drift_reason is not None:
        return ScanRun(
            scan_id=scan_id,
            commit_sha=commit_sha,
            historical_findings=historical_findings,
            reproduced_findings=(),
            matched=(),
            historical_only=tuple(f.finding_id for f in historical_findings),
            reproduced_only=(),
            prompt_prefix_hash_matched=(),
            prompt_prefix_hash_mismatched=(),
            verdict="version_drift",
            drift_reason=drift_reason,
        )

    # Safety net: older scans may not carry changeset_paths in extra.
    # Treat a missing changeset as a missing anchor for replay purposes
    # (we cannot deterministically re-run without the path list).
    if "changeset_paths" not in extra:
        return _missing_anchor(scan_id)

    # -- Rehydrate ChangeSet and re-run pipeline (AC 40) --------------------
    # Lazy imports avoid cycles (PROACTIVE-09): the funnel pipeline
    # transitively imports the telemetry package, and this module
    # lives inside that package. The function-body imports are the
    # established Python pattern for breaking circular deps —
    # explicitly tolerated by the workflow / analyzer / crawler
    # subsystem-isolation tests.
    #
    # ``run_scan_fn`` injection (PROACTIVE-09): when a caller passes a
    # callable for ``run_scan_fn``, the funnel import is skipped
    # entirely. Lets tests mock the seam without dragging the full
    # funnel module-load chain into the test process.
    from autofix.events.schema import ChangeSet
    if run_scan_fn is None:
        from autofix.funnel.pipeline import run_scan as run_scan_fn  # noqa: PLW0603

    raw_paths = extra.get("changeset_paths") or []
    if not isinstance(raw_paths, (list, tuple)):
        raw_paths = []
    watcher_confidence = extra.get("changeset_watcher_confidence", "low")
    if not isinstance(watcher_confidence, str):
        watcher_confidence = "low"

    try:
        changeset = ChangeSet(
            paths=tuple(str(p) for p in raw_paths),
            watcher_confidence=watcher_confidence,
            is_fresh_instance=False,
        )
    except (TypeError, ValueError):
        # Bad changeset data is an anchor-integrity failure, not a
        # scan-stopping crash.
        return _missing_anchor(scan_id)

    try:
        with replay_mode():
            scan_result = run_scan_fn(root, changeset, scan_id)
    except Exception:
        # Any pipeline failure during replay maps to a "mismatch" verdict
        # with no reproduced findings (AC 36 "else map every failure to a
        # verdict"). The historical findings survive so the caller can
        # still diff.
        return ScanRun(
            scan_id=scan_id,
            commit_sha=commit_sha,
            historical_findings=historical_findings,
            reproduced_findings=(),
            matched=(),
            historical_only=tuple(f.finding_id for f in historical_findings),
            reproduced_only=(),
            prompt_prefix_hash_matched=(),
            prompt_prefix_hash_mismatched=(),
            verdict="mismatch",
            drift_reason=None,
        )

    reproduced_findings: tuple[CandidateFinding, ...] = tuple(
        getattr(scan_result, "findings", ()) or ()
    )

    # -- Match historical vs reproduced (AC 44) -----------------------------
    # Composite key: (rule_id, primary_symbol). Reproduced side composes
    # primary_symbol as f"{candidate.path}::{candidate.symbol_name}".
    hist_by_key: dict[tuple[str, str], HistoricalFinding] = {
        (h.rule_id, h.primary_symbol): h for h in historical_findings
    }
    repr_by_key: dict[tuple[str, str], CandidateFinding] = {}
    for cand in reproduced_findings:
        key = (cand.rule_id, f"{cand.path}::{cand.symbol_name}")
        # Preserve first-seen on duplicate keys so the matching is
        # deterministic; duplicates are a pipeline bug but must not crash
        # the replay.
        repr_by_key.setdefault(key, cand)

    matched_pairs: list[tuple[str, str]] = []
    hash_matched: list[str] = []
    hash_mismatched: list[tuple[str, str, str]] = []

    # Iterate historical in insertion order so the output is stable.
    for key, hist in hist_by_key.items():
        cand = repr_by_key.get(key)
        if cand is None:
            continue
        matched_pairs.append((hist.finding_id, cand.finding_id))
        # Recompute prompt_prefix_hash on the reproduced side (AC 45).
        reproduced_hash = _recompute_prompt_prefix_hash(cand)
        if reproduced_hash == hist.prompt_prefix_hash:
            hash_matched.append(hist.finding_id)
        else:
            hash_mismatched.append(
                (hist.finding_id, hist.prompt_prefix_hash, reproduced_hash)
            )

    historical_only = tuple(
        h.finding_id
        for k, h in hist_by_key.items()
        if k not in repr_by_key
    )
    reproduced_only = tuple(
        c.finding_id
        for k, c in repr_by_key.items()
        if k not in hist_by_key
    )

    verdict: Literal["match", "mismatch", "missing_anchor", "version_drift"]
    if historical_only or reproduced_only or hash_mismatched:
        verdict = "mismatch"
    else:
        verdict = "match"

    return ScanRun(
        scan_id=scan_id,
        commit_sha=commit_sha,
        historical_findings=historical_findings,
        reproduced_findings=reproduced_findings,
        matched=tuple(matched_pairs),
        historical_only=historical_only,
        reproduced_only=reproduced_only,
        prompt_prefix_hash_matched=tuple(hash_matched),
        prompt_prefix_hash_mismatched=tuple(hash_mismatched),
        verdict=verdict,
        drift_reason=None,
    )


# -- Helpers ----------------------------------------------------------------


def _missing_anchor(scan_id: str) -> ScanRun:
    """Build the AC-35 missing-anchor ``ScanRun``.

    All tuples empty, ``commit_sha=""``, ``drift_reason=None``. The caller
    does NOT raise in this branch — AC 35 pins the shape.
    """
    return ScanRun(
        scan_id=scan_id,
        commit_sha="",
        historical_findings=(),
        reproduced_findings=(),
        matched=(),
        historical_only=(),
        reproduced_only=(),
        prompt_prefix_hash_matched=(),
        prompt_prefix_hash_mismatched=(),
        verdict="missing_anchor",
        drift_reason=None,
    )


def _parse_historical_findings(
    rows: list[dict],
) -> list[HistoricalFinding]:
    """Extract :class:`HistoricalFinding` entries from the filtered rows.

    We stitch together partial views of the same finding across three
    event types:

    * ``CandidateFindingProduced`` / ``EvidencePacketBuilt`` — carries
      ``rule_id``, ``primary_symbol``, ``finding_id`` and (for the built
      packet) ``prompt_prefix_hash``.
    * ``LLMCallGated`` — carries ``prompt_prefix_hash`` and may carry
      ``rule_id`` / ``primary_symbol`` (fallback population).
    * ``PriorityScored`` — carries ``priority``.

    Missing fields fall back to sensible defaults ("" / 0.0) rather than
    dropping the finding; the downstream matcher relies only on
    ``(rule_id, primary_symbol)``, and the hash-recompute step surfaces
    any hash absence as a mismatch.
    """
    partial: dict[str, dict] = {}
    insertion_order: list[str] = []

    for row in rows:
        event_type = row.get("event_type")
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        finding_id = payload.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id:
            continue
        slot = partial.get(finding_id)
        if slot is None:
            slot = {}
            partial[finding_id] = slot
            insertion_order.append(finding_id)

        if event_type in ("CandidateFindingProduced", "EvidencePacketBuilt"):
            _set_if_absent(slot, "rule_id", payload.get("rule_id"))
            _set_if_absent(slot, "primary_symbol", payload.get("primary_symbol"))
            _set_if_absent(
                slot, "prompt_prefix_hash", payload.get("prompt_prefix_hash")
            )
        elif event_type == "LLMCallGated":
            _set_if_absent(
                slot, "prompt_prefix_hash", payload.get("prompt_prefix_hash")
            )
            _set_if_absent(slot, "rule_id", payload.get("rule_id"))
            _set_if_absent(slot, "primary_symbol", payload.get("primary_symbol"))
        elif event_type == "PriorityScored":
            priority_raw = payload.get("priority")
            try:
                slot.setdefault("priority", float(priority_raw))
            except (TypeError, ValueError):
                slot.setdefault("priority", 0.0)

    findings: list[HistoricalFinding] = []
    for finding_id in insertion_order:
        slot = partial[finding_id]
        findings.append(
            HistoricalFinding(
                rule_id=str(slot.get("rule_id", "") or ""),
                primary_symbol=str(slot.get("primary_symbol", "") or ""),
                finding_id=finding_id,
                prompt_prefix_hash=str(slot.get("prompt_prefix_hash", "") or ""),
                priority=float(slot.get("priority", 0.0) or 0.0),
            )
        )
    return findings


def _set_if_absent(slot: dict, key: str, value: object) -> None:
    """Store ``value`` under ``key`` only if the slot does not yet have a
    non-empty string there. Guards against overwriting an earlier
    authoritative value with a later event that carries the same field
    but a blank default."""
    if value is None:
        return
    if not isinstance(value, str):
        slot.setdefault(key, value)
        return
    if not value:
        return
    existing = slot.get(key)
    if existing is None or (isinstance(existing, str) and not existing):
        slot[key] = value


def _compute_policy_sha(root: Path) -> str:
    """Return the sha256 of ``.autofix/autofix-policy.json`` or ``""``.

    A missing policy file yields the empty string; so does any read
    error. The version-drift check compares this against the historical
    anchor's ``policy_sha256`` field as a byte-equality test.
    """
    policy_path = Path(root) / ".autofix" / "autofix-policy.json"
    if not policy_path.is_file():
        return ""
    try:
        return hashlib.sha256(policy_path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _compute_analyzer_version() -> str:
    """Return the currently-installed cheap-analyzer ``RULE_VERSION``.

    Falls back to ``"v1"`` when the analyzer module cannot be imported;
    the fallback string is deliberately not the empty string so it
    compares unequal to a historical "" (which would indicate the
    anchor was written before version pinning was wired in).
    """
    try:
        from autofix.analyzers.cheap import unused_import

        return str(getattr(unused_import, "RULE_VERSION", "v1") or "v1")
    except Exception:
        return "v1"


_CACHED_TREE_SITTER_VERSION: str | None = None


def _compute_tree_sitter_version() -> str:
    """Return ``tree_sitter.__version__`` (cached), else ``"unknown"``.

    Cached on first call because resolving the version imports the native
    extension, which is slow and repeats for every replay otherwise.
    """
    global _CACHED_TREE_SITTER_VERSION
    if _CACHED_TREE_SITTER_VERSION is not None:
        return _CACHED_TREE_SITTER_VERSION
    try:
        import tree_sitter

        _CACHED_TREE_SITTER_VERSION = str(
            getattr(tree_sitter, "__version__", "unknown") or "unknown"
        )
    except Exception:
        _CACHED_TREE_SITTER_VERSION = "unknown"
    return _CACHED_TREE_SITTER_VERSION


def _recompute_prompt_prefix_hash(cand: CandidateFinding) -> str:
    """Recompute the prompt-prefix hash that ``build_packet`` would stamp
    for ``cand``. Returns ``""`` on any failure so the caller records a
    mismatch rather than crashing.

    AC 45 requires "same recipe as production" — the hash depends on the
    full packet dict including the populated ``analyzer_traces`` entry
    (engine, engine_version, note). We therefore call
    :func:`autofix.evidence.builder.build_packet` with the same
    ``analyzer_note`` format the pipeline uses (see
    ``funnel/pipeline.py::run_scan`` line 573-576) so the reproduced
    hash matches the historical one byte-for-byte.
    """
    try:
        from autofix.evidence.builder import build_packet

        packet = build_packet(
            rule_id=cand.rule_id,
            relpath=cand.path,
            symbol_name=cand.symbol_name,
            normalized_import=getattr(cand, "normalized_import", "") or "",
            changed_slice=getattr(cand, "changed_slice", "") or "",
            analyzer_note=(
                f"bound name {cand.symbol_name} has zero identifier "
                "references in file"
            ),
        )
        return packet.prompt_prefix_hash
    except Exception:
        return ""


__all__ = [
    "HistoricalFinding",
    "ScanRun",
    "replay",
    "replay_mode",
    "_REPLAY_ACTIVE",
    "_REPLAY_EVENTS_SINK",
]
