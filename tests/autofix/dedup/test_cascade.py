"""Unit tests for ``autofix.dedup.cascade``.

Covers AC #15 (classify signature), #16 (tier-1 exact fingerprint match),
#17 (tier-2 SimHash Hamming ≤ 3), #19 (new-cluster registration when no
tier matches), #27 / #28 (cascade does not emit telemetry).
"""
from __future__ import annotations

import autofix.dedup.cascade as cascade_mod
from autofix.dedup.cascade import DedupCascade, DedupDecision
from autofix.dedup.cluster_store import ClusterStore
from autofix.evidence.schema import CandidateFinding
from autofix.ranking.priority_scorer import PriorityScorer


def _make_finding(
    *,
    rule_id: str = "unused-import",
    path: str = "pkg/mod.py",
    symbol_name: str = "my_func",
    normalized_import: str = "os",
    start_line: int = 10,
    end_line: int = 10,
    changed_slice: str = "import os",
    finding_id: str = "fp_aaa",
) -> CandidateFinding:
    return CandidateFinding(
        rule_id,
        path,
        symbol_name,
        normalized_import,
        start_line,
        end_line,
        changed_slice,
        finding_id,
    )


def _score_for(finding: CandidateFinding, store: ClusterStore):
    """Build a PriorityScore with stub graph so cascade has a valid score arg."""
    from types import SimpleNamespace

    graph = SimpleNamespace(
        symbol_count=0,
        all_symbols=frozenset(),
        callers_of=lambda sids, max_depth=2: frozenset(),
    )
    return PriorityScorer().score(finding, graph, store)


def test_tier1_exact_fingerprint_wins() -> None:
    """AC #15, #16: tier-1 fingerprint-match reuses the existing cluster."""
    store = ClusterStore()
    first = _make_finding(finding_id="fp_aaa")
    cid = store.register_new_cluster(first, simhash=0x1234)

    second = _make_finding(finding_id="fp_aaa")
    cascade = DedupCascade()
    decision = cascade.classify(second, _score_for(second, store), store)

    assert decision.tier == 1
    assert decision.novelty == 0.0
    assert decision.is_new_cluster is False
    assert decision.cluster_id == cid


def test_tier2_simhash_hamming_match(monkeypatch) -> None:
    """AC #17: tier-2 matches an existing cluster at Hamming distance ≤ 3."""
    store = ClusterStore()
    seed = _make_finding(finding_id="fp_seed")
    cid = store.register_new_cluster(seed, simhash=0)

    second = _make_finding(finding_id="fp_other")
    monkeypatch.setattr(
        cascade_mod, "compute_simhash", lambda finding, parse_result: 7
    )

    cascade = DedupCascade()
    decision = cascade.classify(second, _score_for(second, store), store)

    assert decision.tier == 2
    assert decision.novelty == 0.0
    assert decision.is_new_cluster is False
    assert decision.cluster_id == cid


def test_tier2_simhash_outside_distance_no_match(monkeypatch) -> None:
    """AC #17 boundary: Hamming distance > 3 must NOT match at tier 2."""
    store = ClusterStore()
    seed = _make_finding(finding_id="fp_seed2")
    store.register_new_cluster(seed, simhash=0)

    second = _make_finding(finding_id="fp_far")
    monkeypatch.setattr(
        cascade_mod, "compute_simhash", lambda finding, parse_result: 0b1111
    )

    cascade = DedupCascade()
    decision = cascade.classify(second, _score_for(second, store), store)

    assert decision.tier == 0
    assert decision.novelty == 1.0
    assert decision.is_new_cluster is True


def test_register_new_cluster_on_empty_store() -> None:
    """AC #19: empty store + first finding → tier=0, novelty=1.0, new cluster."""
    store = ClusterStore()
    assert store.cluster_count == 0

    finding = _make_finding(finding_id="fp_fresh")
    cascade = DedupCascade()
    decision = cascade.classify(finding, _score_for(finding, store), store)

    assert decision.tier == 0
    assert decision.novelty == 1.0
    assert decision.is_new_cluster is True
    assert store.cluster_count == 1


def test_cascade_does_not_emit_telemetry(monkeypatch) -> None:
    """AC #27, #28: cascade.classify must NEVER call events_log.append_event.

    Pipeline (seg-6) owns emission of PriorityScored / FindingDeduped.
    The cascade itself must stay telemetry-free.
    """
    import autofix.telemetry.events_log as events_log

    calls: list[tuple] = []

    def _spy(*args, **kwargs):  # pragma: no cover - asserts zero calls
        calls.append((args, kwargs))
        return "evt_spy"

    monkeypatch.setattr(events_log, "append_event", _spy)

    store = ClusterStore()
    finding = _make_finding(finding_id="fp_tel")

    cascade = DedupCascade()
    _ = cascade.classify(finding, _score_for(finding, store), store)

    assert calls == []


def test_decision_shape_is_frozen_dataclass() -> None:
    """AC #15 implicit: DedupDecision is a frozen dataclass with named fields."""
    from dataclasses import fields, is_dataclass

    assert is_dataclass(DedupDecision)
    assert DedupDecision.__dataclass_params__.frozen is True
    names = [f.name for f in fields(DedupDecision)]
    assert names == ["cluster_id", "tier", "novelty", "is_new_cluster"]
