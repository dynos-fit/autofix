"""Unit tests for ``autofix_next.dedup.cascade``.

Covers AC #15 (classify signature), #16 (tier-1 exact fingerprint match),
#17 (tier-2 SimHash Hamming ≤ 3), #18 (tier-3 skipped when
embedding unavailable), #19 (new-cluster registration when no tier matches),
#27 / #28 (cascade does not emit telemetry).
"""
from __future__ import annotations

import autofix_next.dedup.cascade as cascade_mod
from autofix_next.dedup.cascade import DedupCascade, DedupDecision
from autofix_next.dedup.cluster_store import ClusterStore
from autofix_next.evidence.schema import CandidateFinding
from autofix_next.ranking.priority_scorer import PriorityScorer


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
    """AC #15, #16: tier-1 fingerprint-match reuses the existing cluster.

    Seed the store with a finding having finding_id='fp_aaa'. A second
    classify call with the SAME finding_id must hit tier 1 and return
    (tier=1, novelty=0.0, is_new_cluster=False) against the same cluster.
    """
    store = ClusterStore()
    first = _make_finding(finding_id="fp_aaa")
    cid = store.register_new_cluster(first, simhash=0x1234, embedding=None)

    second = _make_finding(finding_id="fp_aaa")
    cascade = DedupCascade()
    decision = cascade.classify(second, _score_for(second, store), store)

    assert decision.tier == 1
    assert decision.novelty == 0.0
    assert decision.is_new_cluster is False
    assert decision.cluster_id == cid


def test_tier2_simhash_hamming_match(monkeypatch) -> None:
    """AC #17: tier-2 matches an existing cluster at Hamming distance ≤ 3.

    Seed a cluster with simhash_signature=0. Monkeypatch compute_simhash
    in the cascade module so the second finding's signature differs by
    exactly 3 bits (0b111 == 7), which is at the boundary of the match.
    The second finding must land on the seeded cluster via tier 2.
    """
    store = ClusterStore()
    seed = _make_finding(finding_id="fp_seed")
    cid = store.register_new_cluster(seed, simhash=0, embedding=None)

    # Second finding: different finding_id so tier-1 misses; simhash=7
    # yields hamming_distance(0, 7) == 3 — exactly at the tier-2 threshold.
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
    """AC #17 boundary: Hamming distance > 3 must NOT match at tier 2.

    With the embedding tier unavailable in this environment, a distance-4
    signature falls through to tier 0 (new cluster), not tier 2.
    """
    store = ClusterStore()
    # Force the embedding tier off so tier 3 is unambiguously skipped.
    store.embedding_tier_available = False

    seed = _make_finding(finding_id="fp_seed2")
    store.register_new_cluster(seed, simhash=0, embedding=None)

    second = _make_finding(finding_id="fp_far")
    # hamming_distance(0, 0b1111) == 4, which is > SIMHASH_MAX_HAMMING=3.
    monkeypatch.setattr(
        cascade_mod, "compute_simhash", lambda finding, parse_result: 0b1111
    )

    cascade = DedupCascade()
    decision = cascade.classify(second, _score_for(second, store), store)

    assert decision.tier == 0
    assert decision.novelty == 1.0
    assert decision.is_new_cluster is True


def test_tier3_skipped_when_embedding_unavailable(monkeypatch) -> None:
    """AC #18, #19: tier-3 is skipped when store.embedding_tier_available is False.

    The cascade must NOT import or call ``embed_text`` in this branch and
    must fall through to register_new_cluster with tier=0, novelty=1.0.
    """
    store = ClusterStore()
    store.embedding_tier_available = False

    finding = _make_finding(finding_id="fp_te3")
    # Force tier-2 to miss by returning a signature that matches nothing
    # (store is empty anyway, but being explicit).
    monkeypatch.setattr(
        cascade_mod, "compute_simhash", lambda finding, parse_result: 0xABCD
    )

    # Sentinel: if cascade reaches tier 3 despite availability=False, the
    # attribute access would fail here.
    def _boom(*args, **kwargs):
        raise AssertionError(
            "embed_text must not be called when embedding_tier_available is False"
        )

    # embed_text is lazy-imported inside the tier-3 branch; patch the
    # embedding module so any reach-in would trip the boom.
    import autofix_next.dedup.embedding as embedding_mod

    monkeypatch.setattr(embedding_mod, "embed_text", _boom, raising=False)

    cascade = DedupCascade()
    decision = cascade.classify(finding, _score_for(finding, store), store)

    assert decision.tier == 0
    assert decision.novelty == 1.0
    assert decision.is_new_cluster is True
    assert store.cluster_count == 1


def test_register_new_cluster_on_empty_store() -> None:
    """AC #19: empty store + first finding → tier=0, novelty=1.0, new cluster.

    Post-condition: store.cluster_count increases from 0 to 1.
    """
    store = ClusterStore()
    # Force tier-3 off so the behaviour is deterministic on CI machines with
    # the [dedup] extras installed.
    store.embedding_tier_available = False

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

    Pipeline (seg-6) owns emission of PriorityScored / FindingDeduped /
    DedupEmbeddingTierStatus. The cascade itself must stay telemetry-free.
    """
    import autofix_next.telemetry.events_log as events_log

    calls: list[tuple] = []

    def _spy(*args, **kwargs):  # pragma: no cover - asserts zero calls
        calls.append((args, kwargs))
        return "evt_spy"

    monkeypatch.setattr(events_log, "append_event", _spy)

    store = ClusterStore()
    store.embedding_tier_available = False
    finding = _make_finding(finding_id="fp_tel")

    cascade = DedupCascade()
    _ = cascade.classify(finding, _score_for(finding, store), store)

    assert calls == []


def test_decision_shape_is_frozen_dataclass() -> None:
    """AC #15 implicit: DedupDecision is a dataclass (named fields for telemetry).

    Implicit-req from spec.md: DedupDecision must be a dataclass, not a tuple,
    so serialization can access fields by name.
    """
    from dataclasses import fields, is_dataclass

    assert is_dataclass(DedupDecision)
    assert DedupDecision.__dataclass_params__.frozen is True
    names = [f.name for f in fields(DedupDecision)]
    assert names == ["cluster_id", "tier", "novelty", "is_new_cluster"]
