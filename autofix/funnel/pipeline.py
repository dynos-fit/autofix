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

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from autofix import languages
from autofix.analyzers.cheap.unused_import import analyze as _analyze_unused
from autofix.analyzers.linter_passthrough.eslint import analyze as _analyze_eslint
from autofix.analyzers.linter_passthrough.golangci import analyze as _analyze_golangci
from autofix.analyzers.linter_passthrough.ruff import analyze as _analyze_ruff
from autofix.analyzers.linter_passthrough.mypy import analyze as _analyze_mypy
from autofix.analyzers.llm_judgment.code_quality import CodeQualityJudgmentAnalyzer
from autofix.dedup.cascade import DedupCascade, DedupDecision
from autofix.dedup.cluster_store import ClusterStore
from autofix.evidence.builder import build_packet
from autofix.evidence.schema import CandidateFinding
from autofix.events.schema import ChangeSet
from autofix.indexing.embedding import EmbeddingSidecar, SymbolRecall
from autofix.indexing.symbols import build_symbol_table
from autofix.invalidation.call_graph import CallGraph
from autofix.invalidation.planner import (
    DEFAULT_CALLER_DEPTH,
    Invalidation,
    plan as _plan_invalidation,
)
from autofix.llm.scheduler import ScheduleDecision, Scheduler
from autofix.migration import load_legacy_findings
from autofix.parsing.tree_sitter import parse_file, ParseResult
from autofix.ranking.priority_scorer import PriorityScore, PriorityScorer
from autofix.telemetry import events_log
from autofix.telemetry.correlation import (
    current_commit_sha,
    current_event_id,
)

# AC-9: analyzer registry mapping analyzer set names to their analyze callables.
_ANALYZER_REGISTRY: dict[str, object] = {
    "cheap": _analyze_unused,
    "linter:eslint": _analyze_eslint,
    "linter:golangci": _analyze_golangci,
    "linter:ruff": _analyze_ruff,
    "linter:mypy": _analyze_mypy,
    "llm:code-quality": CodeQualityJudgmentAnalyzer.analyze,
}


def _reset_passthrough_analyzer_state() -> None:
    """Clear per-scan memo dicts of every passthrough adapter.

    Audit SEC-RUFF-02 / cq-002 / SEC-RUFF-02-INCOMPLETE: must run on
    both success and exception paths so a long-running daemon does not
    leak one memo entry per scan_id when ``run_scan`` raises. Cleanup
    must never raise — operators see a leak eventually rather than a
    hard failure now.
    """
    try:
        from autofix.analyzers.linter_passthrough import eslint as _eslint
        _eslint._reset_per_scan_state()
    except Exception:
        pass
    try:
        from autofix.analyzers.linter_passthrough import golangci as _golangci
        _golangci._reset_per_scan_state()
    except Exception:
        pass
    try:
        from autofix.analyzers.linter_passthrough import (
            ruff as _linter_ruff_mod,
        )
        _linter_ruff_mod._reset_per_scan_state()
    except Exception:
        pass
    try:
        from autofix.analyzers.linter_passthrough import (
            mypy as _linter_mypy_mod,
        )
        _linter_mypy_mod._reset_per_scan_state()
    except Exception:
        pass
    try:
        from autofix.analyzers.llm_judgment import _base as _llm_judgment_base
        _llm_judgment_base.LLMJudgmentAnalyzer._reset_per_scan_state()
    except Exception:
        pass


def _with_per_scan_cleanup(func):
    """Decorator: wrap ``run_scan`` so per-scan analyzer memos are
    always cleared, including on the exception path. Equivalent to a
    function-body-wide ``try/finally`` but does not require
    re-indenting the body."""
    from functools import wraps

    @wraps(func)
    def _wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        finally:
            _reset_passthrough_analyzer_state()

    return _wrapper


def _legacy_migration_enabled(policy: dict) -> bool:
    """Return whether the legacy-findings injection should run for this scan.

    task-20260506-001 AC 10. The default is ``True`` (no policy / missing
    key means legacy injection is on). Only the JSON boolean ``false``
    disables; truthy non-bool values (e.g. the string ``"false"``) leave
    injection enabled because Python evaluates a non-empty string as True.
    """
    if not isinstance(policy, dict):
        return True
    state_migration = policy.get("state_migration", {})
    if not isinstance(state_migration, dict):
        return True
    return bool(state_migration.get("legacy_findings_enabled", True))


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
    :func:`autofix.dedup.embedding.probe_embedding_tier`
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


def _emit_scan_explanation_event(
    root: Path,
    *,
    scan_id: str,
    invalidation: Invalidation,
    all_findings: list[CandidateFinding],
    scored_items: list[tuple[CandidateFinding, object, PriorityScore, DedupDecision]],
    decisions: list[ScheduleDecision],
    cluster_store: ClusterStore,
) -> None:
    """Append one ``ScanExplanation`` envelope row per scan (AC #21-28).

    Emits a summary row answering the six operator questions (diff wrong,
    invalidation wrong, analyzer noisy, ranking bad, dedup collision, LLM
    too permissive). The payload shape is frozen at EXACTLY the 13 top-level
    keys enumerated in AC #22; adding or removing a key is a contract
    violation enforced by test.

    Telemetry loss follows the OSError-only discipline of the sibling
    ``_emit_*_event`` helpers: a failed disk write must not abort the scan.
    """
    # AC #22: trace/span id from the currently active OTel span; fall back
    # to empty strings when no provider is installed or the span context is
    # invalid. The ``opentelemetry.trace`` API is a hard dependency (seg-1
    # added it to pyproject); the import is local to keep the helper cheap
    # when not called.
    try:
        from opentelemetry.trace import get_current_span

        span_ctx = get_current_span().get_span_context()
        if getattr(span_ctx, "is_valid", False):
            span_id_hex = format(span_ctx.span_id, "016x")
            trace_id_hex = format(span_ctx.trace_id, "032x")
        else:
            span_id_hex = ""
            trace_id_hex = ""
    except Exception:
        # Defensive: any OTel-layer failure must not block telemetry.
        span_id_hex = ""
        trace_id_hex = ""

    # AC #23: ``diff_match`` is True iff the change-detector ran (we only
    # reach this helper when it did — NotAGitRepoError short-circuits the
    # CLI before ``run_scan`` is called) AND ``current_commit_sha()`` is a
    # non-empty string. The equality-with-ScanStarted constraint is
    # satisfied because ``scan_command.py`` stamps the same contextvar
    # value into the ScanStarted row's ``commit_sha`` field.
    resolved_commit_sha = current_commit_sha() or ""
    diff_match = bool(resolved_commit_sha)

    # AC #24: invalidation_plan_size is the FROZENSET length of affected
    # symbols — NOT the file count.
    invalidation_plan_size = len(invalidation.affected_symbols)

    # AC #25: pre-rank candidate count (before dedup cascade).
    analyzer_finding_count = len(all_findings)

    # AC #26: median priority across scored items; 0.0 when empty.
    if scored_items:
        ranking_percentile = statistics.median(
            [score.priority for (_f, _p, score, _d) in scored_items]
        )
    else:
        ranking_percentile = 0.0

    # AC #27: max cluster size across scored items; 0 when empty or when no
    # matching cluster is found. ``ClusterStore.cluster_size`` does not
    # exist today (plan "Open Questions" #3), so we fall back to
    # ``len(cluster.member_fingerprints)`` via a lookup over the cluster
    # list. The seam is narrow: if the method lands later, callers here
    # can switch to ``cluster_store.cluster_size(cid)`` unchanged.
    cluster_by_id: dict[str, object] = {
        c.cluster_id: c for c in cluster_store.clusters
    }
    max_cluster_size = 0
    for (_f, _p, _s, decision) in scored_items:
        cluster = cluster_by_id.get(decision.cluster_id)
        if cluster is None:
            continue
        size = len(getattr(cluster, "member_fingerprints", []) or [])
        if size > max_cluster_size:
            max_cluster_size = size
    dedup_cluster_size = max_cluster_size

    # AC #28: bucket ScheduleDecision.decision values into three counts
    # whose sum equals len(decisions). "confirmed" is the promoted-set;
    # "rejected" is ``promoted_failed``; everything else (skipped_*,
    # cache_store_failed) lands in "skipped".
    _CONFIRMED = {"promoted", "promoted_cache_hit", "promoted_default_tier"}
    confirmed = 0
    rejected = 0
    skipped = 0
    for d in decisions:
        verdict = d.decision
        if verdict in _CONFIRMED:
            confirmed += 1
        elif verdict == "promoted_failed":
            rejected += 1
        else:
            skipped += 1
    llm_verdict_histogram = {
        "confirmed": confirmed,
        "rejected": rejected,
        "skipped": skipped,
    }

    # AC #22: exactly 13 top-level keys. Key order is preserved here for
    # diff-friendly events.jsonl rows; JSON serialization is not
    # order-sensitive, but human readability matters for operators.
    payload = {
        "event_type": "ScanExplanation",
        "repo_id": root.name,
        "scan_id": scan_id,
        "commit_sha": resolved_commit_sha,
        "span_id": span_id_hex,
        "trace_id": trace_id_hex,
        "event_id": current_event_id() or "",
        "diff_match": diff_match,
        "invalidation_plan_size": invalidation_plan_size,
        "analyzer_finding_count": analyzer_finding_count,
        "ranking_percentile": ranking_percentile,
        "dedup_cluster_size": dedup_cluster_size,
        "llm_verdict_histogram": llm_verdict_histogram,
    }
    try:
        events_log.append_event(root, "ScanExplanation", payload)
    except OSError:
        # Same contract as the sibling helpers: telemetry loss must not
        # abort the scan. Any non-OSError (ValueError for an unknown event
        # name, TypeError for a non-dict payload) propagates by design —
        # those indicate programmer bugs surfaced during development.
        pass


def _analyze_one_file_python(
    root: Path, relpath: str, analyzers: list[object] | None = None
) -> list[CandidateFinding]:
    """Run parse → symbol-table → analyzer(s) for one Python path.

    When ``analyzers`` is None, uses only the cheap analyzer (backward-compatible).
    When ``analyzers`` is provided, iterates all active analyzers and yields the
    union of their findings. A missing or non-Python file is not a scan-stopping
    error — it is simply a path with zero findings. Parser-level load errors
    (tree-sitter ABI mismatch, etc.) are re-raised so the operator can
    fix the environment; we only swallow per-file IO issues.
    """
    if analyzers is None:
        analyzers = [_analyze_unused]

    target = root / relpath
    if not target.is_file():
        return []
    try:
        parse_result = parse_file(target, repo_root=root)
    except (FileNotFoundError, PermissionError):
        return []
    symbol_table = build_symbol_table(parse_result)

    findings: list[CandidateFinding] = []
    for analyzer in analyzers:
        try:
            analyzer_result = analyzer(parse_result, symbol_table)
            # Handle both list and iterable returns
            if hasattr(analyzer_result, '__iter__') and not isinstance(analyzer_result, list):
                findings.extend(list(analyzer_result))
            else:
                findings.extend(analyzer_result)
        except (NotImplementedError, OSError):
            # Audit cq-001 fix: previously caught bare ``Exception`` which
            # masked real bugs in the cheap analyzer (e.g. an attribute
            # error that should crash the test suite). Narrow the catch
            # to the two error classes we actually expect from analyzer
            # adapters: NotImplementedError (cheap-path-unsupported) and
            # OSError (file-IO failure inside the analyzer).
            pass

    return findings


def _analyze_one_file(
    root: Path, relpath: str, analyzers: list[object] | None = None
) -> list[CandidateFinding]:
    """Dispatch to the registered language adapter for ``relpath``.

    Task-006 (AC #30 / #45): the funnel orchestrator no longer hard-codes
    the Python analyzer chain. Instead, it looks up the adapter by file
    extension via :func:`autofix.languages.lookup_by_extension`.

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

    When ``analyzers`` is provided, it is passed to :func:`_analyze_one_file_python`
    for multi-analyzer dispatch (AC-9).
    """
    adapter = languages.lookup_by_extension(Path(relpath).suffix)
    if adapter is None:
        return []
    if adapter.language == "python":
        return _analyze_one_file_python(root, relpath, analyzers=analyzers)

    # JS/TS and Go dispatch: call active analyzers whose RULE_ID_PREFIX
    # matches the language. Pass a stub ParseResult (path + relpath only)
    # so the adapter can derive root and file path without a full parse.
    # Python path is untouched — AC-24 byte-identical guarantee preserved.
    if adapter.language in ("javascript", "typescript"):
        target_prefix = "linter:eslint"
    elif adapter.language == "go":
        target_prefix = "linter:golangci"
    else:
        target_prefix = None

    if target_prefix is not None and analyzers:
        # Identify active analyzers for this language via registry key prefix.
        matched_analyzers = [
            callable_
            for key, callable_ in _ANALYZER_REGISTRY.items()
            if key.startswith(target_prefix) and callable_ in analyzers
        ]
        if matched_analyzers:
            target = root / relpath
            if not target.is_file():
                return []
            stub_parse_result = ParseResult(
                path=target,
                relpath=relpath,
                source_bytes=b"",
                tree=None,
                lines=[],
            )
            findings: list[CandidateFinding] = []
            for analyzer in matched_analyzers:
                try:
                    result = analyzer(stub_parse_result, None)
                    if hasattr(result, "__iter__") and not isinstance(result, list):
                        findings.extend(list(result))
                    else:
                        findings.extend(result)
                except (NotImplementedError, OSError):
                    pass
            return findings

    # Non-Python adapter with no matching active analyzer: parse for side
    # effect only (telemetry / caches). Swallow per-file IO errors and
    # grammar-missing NotImplementedError (design-decisions.md §4: cheap
    # path may raise this when the tree-sitter grammar is unavailable,
    # ``available`` is False on the adapter).
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


_POLICY_MAX_BYTES: int = 1 << 20  # 1 MiB cap (task-012 SEC-002).


def _load_scan_policy(root: Path) -> dict | None:
    """Decode ``<root>/.autofix/autofix-policy.json`` if present.

    task-012 AC 21/22. Used only as a fallback when the caller did not
    pass ``policy=`` explicitly (e.g. the CLI entry point). Returns
    ``None`` for any IO / JSON failure so the scan degrades to "no
    policy = sidecar disabled" — the same behavior as running with an
    empty policy file.

    Security hardening (task-012 SEC-001/002/003):

    * **SEC-001**: resolve the target path and refuse to read anything
      whose canonical form escapes ``root`` (blocks symlink pivots
      pointing at ``/etc/passwd`` or sibling repos).
    * **SEC-002**: reject files larger than :data:`_POLICY_MAX_BYTES`
      before reading them (cheap DoS guard — normal policies are
      well under a KiB).
    * **SEC-003**: catch :class:`RecursionError` from ``json.loads``
      when presented with a deeply nested JSON bomb.
    """
    policy_path = root / ".autofix" / "autofix-policy.json"
    try:
        root_real = Path(root).resolve(strict=True)
    except OSError:
        return None
    try:
        resolved = policy_path.resolve(strict=True)
    except OSError:
        return None
    try:
        resolved.relative_to(root_real)
    except ValueError:
        # SEC-001: the resolved path escapes the scan root (symlinked
        # out of the repo). Refuse to read.
        return None
    try:
        stat_result = resolved.stat()
    except OSError:
        return None
    if stat_result.st_size > _POLICY_MAX_BYTES:
        # SEC-002: oversize policy file — treat as missing.
        return None
    try:
        raw = resolved.read_bytes()
    except OSError:
        return None
    try:
        import json
        decoded = json.loads(raw)
    except (ValueError, RecursionError):
        # SEC-003: RecursionError from nested JSON bombs is degraded to
        # "no policy" rather than propagated through run_scan.
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _resolve_sidecar_recall_params(
    policy: dict | None,
) -> tuple[int, float]:
    """Resolve (top_k, similarity_threshold) for the sidecar recall stage.

    task-012 AC 21. Missing keys degrade to the documented defaults:
    ``top_k=5``, ``similarity_threshold=0.75``. Invalid types are also
    treated as missing so operator typos do not break the scan.
    """
    top_k = 5
    threshold = 0.75
    if isinstance(policy, dict):
        index_cfg = policy.get("index")
        if isinstance(index_cfg, dict):
            sidecar_cfg = index_cfg.get("embedding_sidecar")
            if isinstance(sidecar_cfg, dict):
                raw_k = sidecar_cfg.get("top_k")
                if isinstance(raw_k, int) and raw_k > 0:
                    top_k = raw_k
                raw_t = sidecar_cfg.get("similarity_threshold")
                if isinstance(raw_t, (int, float)) and 0.0 <= float(raw_t) <= 1.0:
                    threshold = float(raw_t)
    return top_k, threshold


def _sidecar_query_text_for(finding: CandidateFinding) -> str:
    """Build the sidecar-recall query text for a finding (AC 14).

    Prefers ``f"{language}::{symbol_name} {signature}"`` but falls back to
    ``f"{language}::{symbol_name}"`` when the finding lacks ``signature``.
    """
    language = getattr(finding, "language", None) or "python"
    symbol_name = getattr(finding, "symbol_name", "") or ""
    signature = getattr(finding, "signature", "") or ""
    base = f"{language}::{symbol_name}".strip()
    if signature:
        return f"{base} {signature}"
    return base


@_with_per_scan_cleanup
def run_scan(
    root: Path,
    changeset: ChangeSet,
    scan_id: str,
    *,
    scheduler: Scheduler | None = None,
    graph: CallGraph | None = None,
    policy: dict | None = None,
    progress: Callable[[str], None] | None = None,
    analyzer_set: list[str] | None = None,
) -> ScanResult:
    """Analyze the invalidation-planned paths and schedule each finding.

    Parameters
    ----------
    root:
        Repository root; every path in the computed invalidation plan is
        interpreted relative to this directory.
    changeset:
        The set of paths the watcher says may have changed. The
        :func:`autofix.invalidation.planner.plan` function expands
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
    analyzer_set:
        Optional list of analyzer set names (AC-9). When ``None``, uses only
        the cheap analyzer (backward-compatible). When non-None, must be a
        list of names from the registry (e.g. ``["cheap", "linter:ruff"]``).
        Unknown names are logged as "AnalyzerUnknown" events and skipped.

    Returns
    -------
    ScanResult
        Findings and per-finding scheduling decisions; ``sarif_path`` is
        ``None`` (the CLI layer fills it in downstream).
    """
    root = Path(root)

    def _p(msg: str) -> None:
        if progress is not None:
            progress(msg)

    # AC-9: resolve the active analyzer list based on analyzer_set parameter.
    # When None, uses only the cheap analyzer (backward-compatible).
    # Unknown names are logged and skipped (no exception).
    if analyzer_set is None:
        active_analyzers: list[object] = [_analyze_unused]
    else:
        active_analyzers = []
        for name in analyzer_set:
            mod = _ANALYZER_REGISTRY.get(name)
            if mod is None:
                try:
                    events_log.append_event(
                        root,
                        "AnalyzerUnknown",
                        {"analyzer": name, "scan_id": scan_id},
                    )
                except OSError:
                    pass
                continue
            active_analyzers.append(mod)

    # AC #21: build the graph once if the caller didn't supply one. This
    # is the production path — the CLI doesn't cache across invocations,
    # and the graph is only meaningful for a single scan window anyway.
    if graph is None:
        _p("Building call graph...")
        graph = CallGraph.build_from_root(root)
    _p("Planning invalidation...")

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

    # task-012 AC 14: opt-in embedding sidecar sits between analyzer output
    # and the dedup cascade. When the policy flag is off or absent, the
    # sidecar instance is disabled (AC 5) and every public method is a
    # no-op — preserving byte-identical scan output (AC 22). When enabled
    # but deps are missing, __init__ self-disables after emitting a single
    # EmbeddingSidecarDegraded row (AC 6).
    effective_policy = policy if policy is not None else _load_scan_policy(root)
    sidecar = EmbeddingSidecar(root, effective_policy)
    sidecar_top_k, sidecar_threshold = _resolve_sidecar_recall_params(
        effective_policy
    )
    if sidecar.enabled:
        sidecar.load()

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

    # task-20260506-001 (state-migration-legacy-to-next AC 9-13): inject
    # the projected legacy findings into the same scoring + recall +
    # cascade sequence used by the analyzer loop below. Gated by the
    # ``state_migration.legacy_findings_enabled`` policy flag (default
    # True). When the gate is off, ``load_legacy_findings`` is NEVER
    # called — the code path is short-circuited at the helper boundary
    # (AC 10/11). Legacy findings are appended FIRST so a subsequent
    # analyzer finding sharing the same ``finding_id`` lands in Tier 1
    # (exact fingerprint match) per cascade.py:82-89 (AC 13).
    if _legacy_migration_enabled(effective_policy or {}):
        legacy_findings = load_legacy_findings(root, log=None)
        for finding in legacy_findings:
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
            score = scorer.score(finding, graph, cluster_store)
            _emit_priority_scored_event(root, scan_id=scan_id, score=score)
            recall_hits: list[SymbolRecall] = sidecar.recall(
                query_text=_sidecar_query_text_for(finding),
                top_k=sidecar_top_k,
                threshold=sidecar_threshold,
            )
            decision = cascade.classify(
                finding, score, cluster_store, recall_hits=recall_hits
            )
            _emit_finding_deduped_event(
                root,
                scan_id=scan_id,
                finding_id=finding.finding_id,
                decision=decision,
            )
            scored_items.append((finding, packet, score, decision))

    # AC #21: iterate invalidation.affected_files instead of
    # changeset.paths — the planner has already unioned in every file
    # touched transitively by the callers of the changeset's symbols.
    affected_total = len(invalidation.affected_files)
    _p(f"Analyzing {affected_total} file{'' if affected_total == 1 else 's'}...")
    for file_idx, relpath in enumerate(invalidation.affected_files, start=1):
        # Report per-file only when there are enough files to make
        # silence look like a hang. Below 10, the milestone above is
        # sufficient and a per-file line just adds stderr noise.
        if affected_total >= 10 and (
            file_idx == 1 or file_idx == affected_total or file_idx % 10 == 0
        ):
            _p(f"  [{file_idx}/{affected_total}] {relpath}")
        for finding in _analyze_one_file(root, relpath, analyzers=active_analyzers):
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
            # task-012 AC 14: per-finding semantic recall stage. When the
            # sidecar is disabled, .recall() returns []; AC 15 guarantees
            # DedupCascade.classify(recall_hits=[]) is byte-identical to
            # the pre-task cascade call.
            recall_hits: list[SymbolRecall] = sidecar.recall(
                query_text=_sidecar_query_text_for(finding),
                top_k=sidecar_top_k,
                threshold=sidecar_threshold,
            )
            decision = cascade.classify(
                finding, score, cluster_store, recall_hits=recall_hits
            )
            _emit_finding_deduped_event(
                root,
                scan_id=scan_id,
                finding_id=finding.finding_id,
                decision=decision,
            )
            scored_items.append((finding, packet, score, decision))

    # task-012 AC 20: flush any accumulated incremental-update counters
    # before the cluster store is persisted. When the sidecar is disabled
    # this is a no-op; when enabled with zero upserts this is also a no-op
    # (counters are zero from construction). Only a live update batch
    # emits the single-row ``EmbeddingSidecarIncrementalUpdate`` envelope.
    sidecar.flush_incremental_update()

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
    triage_total = len(scored_items)
    if triage_total:
        _p(
            f"Triaging {triage_total} finding{'' if triage_total == 1 else 's'} "
            "with LLM scheduler..."
        )
    for triage_idx, (finding, packet, _score, _dedup_decision) in enumerate(
        scored_items, start=1
    ):
        if triage_total >= 5:
            _p(
                f"  [{triage_idx}/{triage_total}] {finding.path}:"
                f"{finding.start_line} {finding.rule_id}"
            )
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

    # Seg-6 (AC #21-28): emit exactly ONE ScanExplanation row summarizing
    # the scan. Invoked AFTER ``cluster_store.save(root)`` and AFTER the
    # scheduler dispatch loop, BEFORE the ScanResult return — the order
    # lets the helper read the final cluster state and the fully-resolved
    # decision list. OSError-only is swallowed inside the helper.
    _emit_scan_explanation_event(
        root,
        scan_id=scan_id,
        invalidation=invalidation,
        all_findings=all_findings,
        scored_items=scored_items,
        decisions=decisions,
        cluster_store=cluster_store,
    )

    # Audit SEC-RUFF-02 / cq-002 / SEC-RUFF-02-INCOMPLETE: per-scan
    # memo cleanup runs from the @_with_per_scan_cleanup decorator's
    # finally clause, so it covers BOTH the success path (this return)
    # AND the exception path. No inline cleanup needed here.
    return ScanResult(
        scan_id=scan_id,
        findings=all_findings,
        sarif_path=None,
        schedule_decisions=decisions,
    )


__all__ = ["ScanResult", "run_scan"]
