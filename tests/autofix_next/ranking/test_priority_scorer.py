"""Unit tests for ``autofix_next.ranking.priority_scorer``.

Covers AC #4, #5, #6, #8 and the implicit-replay-determinism guarantee.

All graph / cluster_store dependencies are stubbed via ``types.SimpleNamespace``
so these tests exercise ``PriorityScorer.score`` in isolation — no real
CallGraph, no real ClusterStore.
"""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from types import SimpleNamespace

from autofix_next.evidence.schema import CandidateFinding
from autofix_next.ranking.priority_scorer import PriorityScore, PriorityScorer


def _make_finding(
    *,
    rule_id: str = "unused-import",
    path: str = "pkg/mod.py",
    symbol_name: str = "my_func",
    normalized_import: str = "os",
    start_line: int = 1,
    end_line: int = 1,
    changed_slice: str = "",
    finding_id: str = "fp_default",
    analyzer_confidence: float = 1.0,
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
        analyzer_confidence=analyzer_confidence,
    )


def _empty_graph(*, symbol_count: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        symbol_count=symbol_count,
        all_symbols=frozenset(),
        callers_of=lambda sids, max_depth=2: frozenset(),
    )


def _empty_store() -> SimpleNamespace:
    return SimpleNamespace(
        is_empty=True,
        find_by_fingerprint=lambda fid: None,
    )


def test_priorityscore_is_frozen_slots_dataclass() -> None:
    """AC #4: PriorityScore must be @dataclass(slots=True, frozen=True).

    Field order must be exactly (finding_id, priority, breakdown).
    """
    assert is_dataclass(PriorityScore)
    assert PriorityScore.__dataclass_params__.frozen is True
    assert hasattr(PriorityScore, "__slots__")
    field_names = [f.name for f in fields(PriorityScore)]
    assert field_names == ["finding_id", "priority", "breakdown"]


def test_priority_equals_weighted_sum() -> None:
    """AC #5: priority == 0.35*impact + 0.25*freshness + 0.20*conf + 0.10*nov + 0.10*owner.

    With an empty graph (impact=0.0), empty cluster store (novelty=1.0), and
    a finding at default analyzer_confidence=1.0, the expected priority is:

        0.35*0.0 + 0.25*0.5 + 0.20*1.0 + 0.10*1.0 + 0.10*0.5 = 0.475
    """
    finding = _make_finding(finding_id="fp_weighted")
    graph = _empty_graph()
    store = _empty_store()

    result = PriorityScorer().score(finding, graph, store)

    expected = (
        0.35 * 0.0
        + 0.25 * 0.5
        + 0.20 * 1.0
        + 0.10 * 1.0
        + 0.10 * 0.5
    )
    assert abs(result.priority - expected) < 1e-9
    assert result.finding_id == "fp_weighted"


def test_breakdown_contains_all_required_keys() -> None:
    """AC #6: breakdown must contain the 8 canonical keys.

    Principal signals: impact, freshness, confidence, novelty, owner_risk.
    Intermediates: impact_raw_callers_count, impact_symbol_count,
    novelty_cluster_match_state.
    """
    finding = _make_finding()
    graph = _empty_graph()
    store = _empty_store()

    result = PriorityScorer().score(finding, graph, store)

    required_keys = {
        "impact",
        "freshness",
        "confidence",
        "novelty",
        "owner_risk",
        "impact_raw_callers_count",
        "impact_symbol_count",
        "novelty_cluster_match_state",
    }
    assert required_keys.issubset(result.breakdown.keys())


def test_symbol_not_in_graph_yields_zero_impact() -> None:
    """AC #8: when finding's symbol is absent from the call graph, impact == 0.0.

    ``symbol_count`` may be large (denominator non-zero) but the symbol itself
    is not registered in ``all_symbols``. This must NOT raise and the breakdown
    ``impact`` entry must be exactly 0.0.
    """
    finding = _make_finding(path="pkg/mod.py", symbol_name="my_func")
    graph = SimpleNamespace(
        symbol_count=10,
        all_symbols=frozenset(),
        callers_of=lambda sids, max_depth=2: frozenset(),
    )
    store = _empty_store()

    result = PriorityScorer().score(finding, graph, store)

    assert result.breakdown["impact"] == 0.0
    assert result.breakdown["impact_raw_callers_count"] == 0.0


def test_priority_deterministic_across_invocations() -> None:
    """Implicit-req: two identical calls produce byte-identical PriorityScore.

    Replay determinism demands that the scorer is a pure function of its
    inputs; no hidden state should leak between invocations.
    """
    finding = _make_finding(finding_id="fp_det", analyzer_confidence=0.7)
    graph = _empty_graph()
    store = _empty_store()

    scorer = PriorityScorer()
    first = scorer.score(finding, graph, store)
    second = scorer.score(finding, graph, store)

    assert first.finding_id == second.finding_id
    assert first.priority == second.priority
    assert first.breakdown == second.breakdown
