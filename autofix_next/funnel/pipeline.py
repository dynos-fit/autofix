"""Funnel pipeline orchestrator.

Wires together the three existing layers for a single scan:

1. For each repo-relative path in the :class:`ChangeSet`, parse with the
   tree-sitter wrapper, build a :class:`SymbolTable`, and run the cheap
   ``unused-import.intra-file`` analyzer.
2. For each :class:`CandidateFinding` produced, build an
   :class:`EvidencePacket` and emit an ``EvidencePacketBuilt`` envelope
   row via the telemetry writer.
3. Hand each packet to the :class:`Scheduler`, which applies the
   suppression + dedup gates and (if promoted) calls the locked LLM
   seam. The scheduler emits its own ``LLMCallGated`` rows.

The SARIF emission step lives in the CLI layer (seg-5). This orchestrator
returns the findings and the per-finding :class:`ScheduleDecision` list
so the CLI can derive SARIF + human output without re-running analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from autofix_next import languages
from autofix_next.analyzers.cheap.unused_import import analyze as _analyze_unused
from autofix_next.dedup.cascade import DedupCascade, DedupDecision
from autofix_next.dedup.cluster_store import ClusterStore
from autofix_next.evidence.builder import build_packet
from autofix_next.evidence.schema import CandidateFinding
from autofix_next.events.schema import ChangeSet
from autofix_next.indexing.symbols import build_symbol_table
from autofix_next.invalidation.call_graph import CallGraph
from autofix_next.invalidation.planner import (
    DEFAULT_CALLER_DEPTH,
    Invalidation,
    plan as _plan_invalidation,
)
from autofix_next.llm.scheduler import ScheduleDecision, Scheduler
from autofix_next.parsing.tree_sitter import parse_file
from autofix_next.ranking.priority_scorer import PriorityScore, PriorityScorer
from autofix_next.telemetry import events_log


@dataclass(slots=True)
class ScanResult:
    """Output of a single :func:`run_scan` invocation.

    Attributes
    ----------
    scan_id:
        Opaque identifier passed in by the CLI; threaded into every
        emitted envelope row for cross-sink correlation.
    findings:
        All :class:`CandidateFinding` objects produced by the analyzer
        pass, in traversal order of the changeset and then source order
        within each file.
    sarif_path:
        Set by the CLI layer (seg-5) after emitting SARIF. Always
        ``None`` when returned by :func:`run_scan` itself.
    schedule_decisions:
        Per-finding :class:`ScheduleDecision`. Index matches ``findings``.
    """

    scan_id: str
    findings: list[CandidateFinding] = field(default_factory=list)
    sarif_path: Path | None = None
    schedule_decisions: list[ScheduleDecision] = field(default_factory=list)


def _emit_packet_built_event(
    root: Path,
    *,
    scan_id: str,
    finding: CandidateFinding,
    prompt_prefix_hash: str,
) -> None:
    """Append one ``EvidencePacketBuilt`` envelope row, swallowing IO errors.

    Telemetry loss must not abort the scan; operators see the gap in the
    tailed events.jsonl rather than a crashed scanner.
    """
    payload = {
        "event_type": "EvidencePacketBuilt",
        "repo_id": root.name,
        "scan_id": scan_id,
        "rule_id": finding.rule_id,
        "finding_id": finding.finding_id,
        "primary_symbol": f"{finding.path}::{finding.symbol_name}",
        "prompt_prefix_hash": prompt_prefix_hash,
    }
    try:
        events_log.append_event(root, "EvidencePacketBuilt", payload)
    except OSError:
        pass


def _emit_invalidation_computed_event(
    root: Path,
    *,
    scan_id: str,
    changeset: ChangeSet,
    invalidation: Invalidation,
    graph: CallGraph,
) -> None:
    """Append one ``InvalidationComputed`` envelope row (AC #22).

    The payload carries EXACTLY the ten keys pinned by the TDD test
    contract — no more, no fewer. Telemetry write failures are swallowed
    the same way :func:`_emit_packet_built_event` swallows them: a lost
    row must never abort the scan.

    The ``source`` field is hard-coded to ``"cli"`` because that is the
    only ingress this orchestrator is invoked from today. Adding a new
    ingress means threading a new arg — a deliberate surface change
    rather than an implicit mutation.
    """
    payload = {
        "event_type": "InvalidationComputed",
        "repo_id": root.name,
        "scan_id": scan_id,
        "source": "cli",
        "watcher_confidence": changeset.watcher_confidence,
        "depth_used": invalidation.depth_used,
        "is_full_sweep": invalidation.is_full_sweep,
        "graph_symbol_count": graph.symbol_count,
        "affected_symbol_count": len(invalidation.affected_symbols),
        "affected_file_count": len(invalidation.affected_files),
    }
    # Seg-2 (AC #13) — thread the SCIP-index cache mode signal into the
    # envelope row when ``build_from_root`` raised it. ``None`` means the
    # index persisted cleanly; ``"fallback_concurrent_writer"`` means a
    # flock timeout forced us to skip the cache write this run. We only
    # add the key when it's set, so clean runs keep the original 10-key
    # payload shape.
    cache_mode = getattr(graph, "last_cache_mode", None)
    if cache_mode is not None:
        payload["index_cache_mode"] = cache_mode
    try:
        events_log.append_event(root, "InvalidationComputed", payload)
    except OSError:
        # Same contract as ``_emit_packet_built_event``: telemetry loss
        # must not abort the scan. Operators see the gap in the tailed
        # events.jsonl rather than a crashed scanner.
        pass


def _emit_priority_scored_event(
    root: Path,
    *,
    scan_id: str,
    score: PriorityScore,
) -> None:
    """Append one ``PriorityScored`` envelope row per finding (AC #31).

    Telemetry loss is swallowed with the same OSError-only discipline used
    by :func:`_emit_packet_built_event` and
    :func:`_emit_invalidation_computed_event`: the scan must continue even
    if the events.jsonl write fails.
    """
    payload = {
        "event_type": "PriorityScored",
        "repo_id": root.name,
        "scan_id": scan_id,
        "finding_id": score.finding_id,
        "priority": score.priority,
        "breakdown": dict(score.breakdown),
    }
    try:
        events_log.append_event(root, "PriorityScored", payload)
    except OSError:
        pass


def _emit_finding_deduped_event(
    root: Path,
    *,
    scan_id: str,
    finding_id: str,
    decision: DedupDecision,
) -> None:
    """Append one ``FindingDeduped`` envelope row per finding (AC #31).

    The payload carries the cascade tier that matched (0 = new cluster,
    1/2/3 = cascade tier), the cluster id, the novelty score, and the
    ``is_new_cluster`` flag. OSError is swallowed.
    """
    payload = {
        "event_type": "FindingDeduped",
        "repo_id": root.name,
        "scan_id": scan_id,
        "finding_id": finding_id,
        "cluster_id": decision.cluster_id,
        "tier_matched": decision.tier,
        "novelty": decision.novelty,
        "is_new_cluster": decision.is_new_cluster,
    }
    try:
        events_log.append_event(root, "FindingDeduped", payload)
    except OSError:
        pass


def _emit_dedup_tier_status_event(
    root: Path,
    *,
    scan_id: str,
    available: bool,
    reason: str,
) -> None:
    """Append one ``DedupEmbeddingTierStatus`` envelope row per scan (AC #31).

    Emitted exactly once per :func:`run_scan` invocation regardless of
    whether any findings are produced. The ``reason`` is one of the
    sentinel strings returned by
    :func:`autofix_next.dedup.embedding.probe_embedding_tier`
    (``"available"``, ``"deps_missing"``,
    ``"model_cache_missing_offline"``).
    """
    payload = {
        "event_type": "DedupEmbeddingTierStatus",
        "repo_id": root.name,
        "scan_id": scan_id,
        "available": available,
        "reason": reason,
    }
    try:
        events_log.append_event(root, "DedupEmbeddingTierStatus", payload)
    except OSError:
        pass


def _emit_cluster_store_persisted_event(
    root: Path,
    *,
    scan_id: str,
    cluster_count: int,
    tier3_enabled: bool,
    cache_mode: str,
) -> None:
    """Append one ``ClusterStorePersisted`` envelope row per scan (AC #31).

    Emitted after :meth:`ClusterStore.save` regardless of whether the
    save wrote cleanly or landed in fallback-concurrent-writer mode.
    ``cache_mode`` reflects ``ClusterStore.last_cache_mode`` (defaulting
    to ``"ok"`` when it is ``None``, i.e. a clean write).
    """
    payload = {
        "event_type": "ClusterStorePersisted",
        "repo_id": root.name,
        "scan_id": scan_id,
        "cluster_count": cluster_count,
        "tier3_enabled": tier3_enabled,
        "cache_mode": cache_mode,
    }
    try:
        events_log.append_event(root, "ClusterStorePersisted", payload)
    except OSError:
        pass


def _analyze_one_file_python(
    root: Path, relpath: str
) -> list[CandidateFinding]:
    """Run parse → symbol-table → analyzer for one Python path.

    Extracted byte-identically from the pre-task-006 ``_analyze_one_file``
    body. A missing or non-Python file is not a scan-stopping error — it
    is simply a path with zero findings. Parser-level load errors
    (tree-sitter ABI mismatch, etc.) are re-raised so the operator can
    fix the environment; we only swallow per-file IO issues.
    """
    target = root / relpath
    if not target.is_file():
        return []
    try:
        parse_result = parse_file(target, repo_root=root)
    except (FileNotFoundError, PermissionError):
        return []
    symbol_table = build_symbol_table(parse_result)
    return _analyze_unused(parse_result, symbol_table)


def _analyze_one_file(
    root: Path, relpath: str
) -> list[CandidateFinding]:
    """Dispatch to the registered language adapter for ``relpath``.

    Task-006 (AC #30 / #45): the funnel orchestrator no longer hard-codes
    the Python analyzer chain. Instead, it looks up the adapter by file
    extension via :func:`autofix_next.languages.lookup_by_extension`.

    * Unknown extension → ``[]`` (no warning).
    * ``adapter.language == "python"`` → delegate to
      :func:`_analyze_one_file_python`, which preserves the exact
      pre-task-006 behavior (AC #31 byte-identical output).
    * Any other adapter → call ``adapter.parse_cheap(...)`` for its side
      effect (telemetry / caches) and return ``[]``. No per-language
      analyzer is registered for JS/TS or Go today (AC #45).

    Per-file IO errors raised by a non-Python adapter's ``parse_cheap``
    (``FileNotFoundError`` / ``PermissionError`` / ``OSError``) are
    swallowed: they are not scan-stopping bugs.
    """
    adapter = languages.lookup_by_extension(Path(relpath).suffix)
    if adapter is None:
        return []
    if adapter.language == "python":
        return _analyze_one_file_python(root, relpath)
    # Non-Python adapter: parse for side effect only; no analyzer
    # registered today. Swallow per-file IO errors and grammar-missing
    # NotImplementedError (design-decisions.md §4: cheap path may raise
    # this when the tree-sitter grammar is unavailable, ``available`` is
    # False on the adapter).
    target = root / relpath
    if not target.is_file():
        return []
    try:
        _ = adapter.parse_cheap(target.read_bytes())
    except (FileNotFoundError, PermissionError, OSError):
        pass
    except NotImplementedError:
        pass
    return []


def run_scan(
    root: Path,
    changeset: ChangeSet,
    scan_id: str,
    *,
    scheduler: Scheduler | None = None,
    graph: CallGraph | None = None,
) -> ScanResult:
    """Analyze the invalidation-planned paths and schedule each finding.

    Parameters
    ----------
    root:
        Repository root; every path in the computed invalidation plan is
        interpreted relative to this directory.
    changeset:
        The set of paths the watcher says may have changed. The
        :func:`autofix_next.invalidation.planner.plan` function expands
        this into the full set of files touched transitively via the
        call graph (AC #21 / #24).
    scan_id:
        Opaque identifier threaded into every emitted envelope row.
    scheduler:
        Optional pre-built :class:`Scheduler`. A fresh one is created
        when ``None`` so the per-scan dedup set starts empty.
    graph:
        Optional pre-built :class:`CallGraph`. When ``None``, a fresh
        graph is built from ``root`` once at the top of the scan. Tests
        and long-lived daemons can pass a reusable graph to skip the
        rebuild. Keyword-only so the positional signature stays compatible
        with the CLI caller.

    Returns
    -------
    ScanResult
        Findings and per-finding scheduling decisions; ``sarif_path`` is
        ``None`` (the CLI layer fills it in downstream).
    """
    root = Path(root)

    # AC #21: build the graph once if the caller didn't supply one. This
    # is the production path — the CLI doesn't cache across invocations,
    # and the graph is only meaningful for a single scan window anyway.
    if graph is None:
        graph = CallGraph.build_from_root(root)

    # AC #21: new planner signature — (graph, changeset, *, max_depth).
    # The old 1-arg identity stub is gone; passing just ``changeset``
    # would raise TypeError (verified by seg-4's
    # ``test_plan_signature_replaces_stub``).
    invalidation = _plan_invalidation(
        graph, changeset, max_depth=DEFAULT_CALLER_DEPTH
    )

    # AC #21 / #22: emit the InvalidationComputed envelope row between
    # ScanStarted (written by scan_command.py) and the per-file analyzer
    # loop below. Telemetry loss is swallowed inside the helper.
    _emit_invalidation_computed_event(
        root,
        scan_id=scan_id,
        changeset=changeset,
        invalidation=invalidation,
        graph=graph,
    )

    resolved_scheduler = scheduler if scheduler is not None else Scheduler(root=root)

    # Seg-6 (AC #31): load the persistent cluster store once per scan,
    # emit the one-shot DedupEmbeddingTierStatus envelope, and construct
    # the scorer + cascade that the analyzer loop below will drive. The
    # load is non-locking (AC #22 from seg-4); a missing store on the
    # first scan yields an empty :class:`ClusterStore` whose
    # ``is_empty`` is True so ``compute_novelty`` resolves to 1.0 for
    # every finding produced (AC #33).
    cluster_store = ClusterStore.load(root)
    _emit_dedup_tier_status_event(
        root,
        scan_id=scan_id,
        available=cluster_store.embedding_tier_available,
        reason=cluster_store.embedding_tier_reason,
    )
    scorer = PriorityScorer()
    cascade = DedupCascade()

    all_findings: list[CandidateFinding] = []
    # Seg-6 (AC #31): we can no longer append to ``decisions`` inside the
    # analyzer loop because the scheduler dispatch is deferred until
    # after we sort the collected packets by priority (descending). We
    # collect the per-finding quad here and build the index-aligned
    # decisions list below, after the scheduler has been driven in
    # priority order.
    scored_items: list[
        tuple[CandidateFinding, object, PriorityScore, DedupDecision]
    ] = []

    # AC #21: iterate invalidation.affected_files instead of
    # changeset.paths — the planner has already unioned in every file
    # touched transitively by the callers of the changeset's symbols.
    for relpath in invalidation.affected_files:
        for finding in _analyze_one_file(root, relpath):
            all_findings.append(finding)
            packet = build_packet(
                rule_id=finding.rule_id,
                relpath=finding.path,
                symbol_name=finding.symbol_name,
                normalized_import=finding.normalized_import,
                changed_slice=finding.changed_slice,
                analyzer_note=(
                    f"bound name {finding.symbol_name} has zero identifier "
                    "references in file"
                ),
            )
            _emit_packet_built_event(
                root,
                scan_id=scan_id,
                finding=finding,
                prompt_prefix_hash=packet.prompt_prefix_hash,
            )
            # Seg-6 (AC #31): score first, then classify. The scorer
            # reads cluster-store state purely to resolve novelty, so it
            # must run before the cascade mutates the store (tier 2/3
            # match -> update_on_match; no-match -> register_new_cluster).
            score = scorer.score(finding, graph, cluster_store)
            _emit_priority_scored_event(root, scan_id=scan_id, score=score)
            decision = cascade.classify(finding, score, cluster_store)
            _emit_finding_deduped_event(
                root,
                scan_id=scan_id,
                finding_id=finding.finding_id,
                decision=decision,
            )
            scored_items.append((finding, packet, score, decision))

    # Seg-6 (AC #31): persist the cluster store exactly once per scan,
    # AFTER every finding has been classified (so every register /
    # update has been applied) and BEFORE scheduler dispatch. The
    # persisted envelope is emitted unconditionally — even on a scan
    # with zero findings — so replayers see a deterministic one-per-scan
    # row. ``last_cache_mode`` is ``None`` on a clean atomic write and
    # ``"fallback_concurrent_writer"`` on a flock timeout (seg-4 AC #21).
    cluster_store.save(root)
    _emit_cluster_store_persisted_event(
        root,
        scan_id=scan_id,
        cluster_count=cluster_store.cluster_count,
        tier3_enabled=cluster_store.embedding_tier_available,
        cache_mode=cluster_store.last_cache_mode or "ok",
    )

    # Seg-6 (AC #31): sort the collected packets by priority DESCENDING
    # and dispatch to the scheduler in that order so the scheduler's
    # dedup gate honours priority precedence (the highest-priority
    # duplicate wins the LLM budget). Ties retain traversal order by
    # virtue of Python's stable sort.
    scored_items.sort(key=lambda item: -item[2].priority)
    decision_by_fp: dict[str, ScheduleDecision] = {}
    for finding, packet, _score, _dedup_decision in scored_items:
        decision_by_fp[finding.finding_id] = resolved_scheduler.schedule(packet)

    # Seg-6 (AC #31): re-align the schedule decisions with
    # ``all_findings`` (analyzer traversal order) so
    # :attr:`ScanResult.schedule_decisions` stays index-aligned with
    # :attr:`ScanResult.findings` as documented in the dataclass
    # docstring. The scheduler was driven in priority order; the return
    # value is flipped back so downstream consumers (CLI / SARIF) do not
    # need to care about the ordering change.
    decisions: list[ScheduleDecision] = [
        decision_by_fp[f.finding_id] for f in all_findings
    ]

    return ScanResult(
        scan_id=scan_id,
        findings=all_findings,
        sarif_path=None,
        schedule_decisions=decisions,
    )


__all__ = ["ScanResult", "run_scan"]
