"""Round-trip dedup test: legacy + new finding with shared finding_id → Tier-1.

Covers AC 13: when a legacy entry and a new analyzer finding share the same
finding_id, DedupCascade.classify returns a Tier-1 exact-fingerprint match
decision on the second call (legacy is processed first, seeding the cluster
store; new analyzer finding is the second call).

This test is authored TDD-first and FAILS at import until
autofix/migration.py is created.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from autofix.dedup.cascade import DedupCascade, DedupDecision
from autofix.dedup.cluster_store import ClusterStore
from autofix.evidence.schema import CandidateFinding
from autofix.ranking.priority_scorer import PriorityScorer
from autofix.migration import load_legacy_findings


def _stub_graph():
    return SimpleNamespace(
        symbol_count=0,
        all_symbols=frozenset(),
        callers_of=lambda sids, max_depth=2: frozenset(),
    )


def _score_for(finding: CandidateFinding, store: ClusterStore):
    return PriorityScorer().score(finding, _stub_graph(), store)


def test_dedup_cascade_round_trip_tier1_match(tmp_path):
    # AC 13
    """Legacy entry processed first seeds the cluster store.
    New analyzer finding with the same finding_id must hit Tier-1
    (DedupDecision.tier == 1, novelty == 0.0, is_new_cluster == False).

    Verifies:
    - DedupDecision.tier constant value '1' matches the Tier-1 constant
      used in autofix/dedup/cascade.py (i.e. integer 1).
    - Byte-equality on finding_id is sufficient for the match.
    """
    SHARED_FINDING_ID = "fp_shared_roundtrip"

    # --- Set up the legacy findings file ---
    autofix_dir = tmp_path / ".autofix" / "state" / "current"
    autofix_dir.mkdir(parents=True)
    findings_file = autofix_dir / "findings.json"
    findings_file.write_text(
        json.dumps({
            "findings": [
                {
                    "finding_id": SHARED_FINDING_ID,
                    "status": "new",
                    "category": "unused-import",
                    "description": "Unused import os",
                    "evidence": {
                        "file": "src/foo.py",
                        "line": 10,
                        "symbol": "os",
                    },
                    "confidence_score": 0.9,
                }
            ]
        }),
        encoding="utf-8",
    )

    # --- Load legacy findings ---
    legacy_findings = load_legacy_findings(tmp_path)
    assert len(legacy_findings) == 1
    legacy_cf = legacy_findings[0]
    assert legacy_cf.finding_id == SHARED_FINDING_ID

    # --- Build a fresh cluster store and cascade ---
    store = ClusterStore()
    store.embedding_tier_available = False  # keep tier-3 out of play

    cascade = DedupCascade()

    # First call: legacy finding → registers a new cluster (tier=0)
    legacy_score = _score_for(legacy_cf, store)
    first_decision = cascade.classify(legacy_cf, legacy_score, store, recall_hits=[])

    assert first_decision.tier == 0, (
        f"Legacy finding should create a new cluster (tier=0), got tier={first_decision.tier}"
    )
    assert first_decision.is_new_cluster is True
    seeded_cluster_id = first_decision.cluster_id

    # --- Simulate a new analyzer finding with the SAME finding_id ---
    new_analyzer_finding = CandidateFinding(
        rule_id="unused-import",
        path="src/foo.py",
        symbol_name="os",
        normalized_import="",
        start_line=10,
        end_line=10,
        changed_slice="src/foo.py:10@os",
        finding_id=SHARED_FINDING_ID,  # identical fingerprint
        analyzer_confidence=1.0,
    )

    # Second call: new finding with same finding_id → must hit Tier-1
    new_score = _score_for(new_analyzer_finding, store)
    second_decision = cascade.classify(
        new_analyzer_finding, new_score, store, recall_hits=[]
    )

    assert second_decision.tier == 1, (
        f"Expected Tier-1 exact-fingerprint match (tier=1), got tier={second_decision.tier}"
    )
    assert second_decision.novelty == 0.0
    assert second_decision.is_new_cluster is False
    assert second_decision.cluster_id == seeded_cluster_id, (
        "Tier-1 match must resolve to the same cluster seeded by the legacy finding"
    )
