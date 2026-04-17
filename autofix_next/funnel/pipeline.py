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

from autofix_next.analyzers.cheap.unused_import import analyze as _analyze_unused
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


def _analyze_one_file(
    root: Path, relpath: str
) -> list[CandidateFinding]:
    """Run parse → symbol-table → analyzer for one path; skip IO failures.

    A missing or non-Python file is not a scan-stopping error — it is
    simply a path with zero findings. Parser-level load errors
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

    all_findings: list[CandidateFinding] = []
    decisions: list[ScheduleDecision] = []

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
            decision = resolved_scheduler.schedule(packet)
            decisions.append(decision)

    return ScanResult(
        scan_id=scan_id,
        findings=all_findings,
        sarif_path=None,
        schedule_decisions=decisions,
    )


__all__ = ["ScanResult", "run_scan"]
