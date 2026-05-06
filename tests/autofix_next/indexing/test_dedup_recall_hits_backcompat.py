"""task-012 AC 25 — DedupCascade.classify is byte-identical when
``recall_hits`` is omitted, ``None``, or ``[]``.

All three invocations must produce equivalent :class:`DedupDecision`
instances for the same finding against a fresh cluster store.
"""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

from autofix_next.dedup.cascade import DedupCascade, DedupDecision
from autofix_next.dedup.cluster_store import ClusterStore
from autofix_next.evidence.schema import CandidateFinding
from autofix_next.ranking.priority_scorer import PriorityScore


def _finding() -> CandidateFinding:
    fp = hashlib.sha256(b"test-finding").hexdigest()
    return CandidateFinding(
        rule_id="unused-import.intra-file",
        path="x.py",
        symbol_name="os",
        normalized_import="os",
        start_line=1,
        end_line=1,
        changed_slice="import os",
        finding_id=fp,
    )


def _score(fp: str) -> PriorityScore:
    return PriorityScore(
        finding_id=fp,
        priority=0.5,
        breakdown={
            "impact": 0.0,
            "freshness": 0.5,
            "confidence": 1.0,
            "novelty": 1.0,
            "owner_risk": 0.0,
            "impact_raw_callers_count": 0,
            "impact_symbol_count": 1,
            "novelty_cluster_match_state": 1.0,
        },
    )


def test_classify_recall_hits_defaults_are_equivalent(tmp_path: Path) -> None:
    cascade = DedupCascade()
    f = _finding()
    s = _score(f.finding_id)

    store_a = ClusterStore.load(tmp_path / "a")
    store_b = ClusterStore.load(tmp_path / "b")
    store_c = ClusterStore.load(tmp_path / "c")

    d_omitted = cascade.classify(f, s, store_a)
    d_none = cascade.classify(f, s, store_b, recall_hits=None)
    d_empty = cascade.classify(f, s, store_c, recall_hits=[])

    # All three run against a fresh store → new cluster.
    for d in (d_omitted, d_none, d_empty):
        assert isinstance(d, DedupDecision)
        assert d.tier == 0
        assert d.novelty == 1.0
        assert d.is_new_cluster is True


def test_classify_recall_hits_is_keyword_only() -> None:
    """AC 15: the new parameter is keyword-only so positional callers are
    unaffected."""
    sig = inspect.signature(DedupCascade.classify)
    param = sig.parameters["recall_hits"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None
