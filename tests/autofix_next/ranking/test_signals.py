"""Unit tests for ``autofix_next.ranking.signals``.

Covers AC #7, #8, #9, #10, #11, #12 and the empty-graph zero-division guard.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

from autofix_next.evidence.schema import CandidateFinding
from autofix_next.ranking.signals import (
    ImpactRaw,
    compute_confidence,
    compute_freshness,
    compute_impact,
    compute_novelty,
    compute_owner_risk,
)


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


def test_compute_impact_symbol_present() -> None:
    """AC #7: normalized == log2(1 + n_callers) / log2(1 + symbol_count).

    With symbol_count=100 and 3 callers, the expected normalized impact is
    ``log2(4) / log2(101)`` and raw_callers_count must equal 3.
    """
    finding = _make_finding(path="pkg/mod.py", symbol_name="my_func")
    sid = "pkg/mod.py::my_func"
    callers = frozenset({"caller_a", "caller_b", "caller_c"})
    graph = SimpleNamespace(
        symbol_count=100,
        all_symbols=frozenset({sid}),
        callers_of=lambda sids, max_depth=2: callers,
    )

    result = compute_impact(finding, graph)

    expected_norm = math.log2(1 + 3) / math.log2(1 + 100)
    assert isinstance(result, ImpactRaw)
    assert abs(result.normalized - expected_norm) < 1e-9
    assert result.raw_callers_count == 3
    assert result.symbol_count == 100


def test_compute_impact_symbol_absent() -> None:
    """AC #8: when the symbol is not in graph.all_symbols, return (0.0, 0, ...).

    symbol_count is preserved in the returned triple so downstream
    breakdown intermediates still record the denominator's cardinality.
    """
    finding = _make_finding(path="pkg/mod.py", symbol_name="missing")
    graph = SimpleNamespace(
        symbol_count=50,
        all_symbols=frozenset({"other/path.py::other"}),
        callers_of=lambda sids, max_depth=2: frozenset(),
    )

    result = compute_impact(finding, graph)

    assert result.normalized == 0.0
    assert result.raw_callers_count == 0
    assert result.symbol_count == 50


def test_compute_impact_zero_symbol_count() -> None:
    """Implicit-req / AC #8 boundary: symbol_count == 0 must not ZeroDivision.

    ``log2(1 + 0) = 0`` would be a zero denominator. compute_impact must
    detect this and return a safe zero triple rather than raise.
    """
    finding = _make_finding()
    graph = SimpleNamespace(
        symbol_count=0,
        all_symbols=frozenset(),
        callers_of=lambda sids, max_depth=2: frozenset(),
    )

    # No exception, returns a well-formed ImpactRaw.
    result = compute_impact(finding, graph)

    assert result.normalized == 0.0
    assert result.raw_callers_count == 0
    assert result.symbol_count == 0


def test_compute_freshness_stub() -> None:
    """AC #9: compute_freshness returns the stub constant 0.5."""
    finding = _make_finding()
    assert compute_freshness(finding) == 0.5


def test_compute_owner_risk_stub() -> None:
    """AC #10: compute_owner_risk returns the stub constant 0.5."""
    finding = _make_finding()
    assert compute_owner_risk(finding) == 0.5


def test_compute_confidence_passthrough() -> None:
    """AC #11: compute_confidence returns finding.analyzer_confidence verbatim.

    Verifies against two distinct findings to rule out any constant-return
    bug in the stub.
    """
    low = _make_finding(finding_id="low", analyzer_confidence=0.3)
    high = _make_finding(finding_id="high", analyzer_confidence=0.9)

    assert compute_confidence(low) == 0.3
    assert compute_confidence(high) == 0.9


def test_compute_novelty_new_cluster() -> None:
    """AC #12: novelty == 1.0 when DedupDecision.is_new_cluster is True."""
    dedup_decision = SimpleNamespace(is_new_cluster=True)
    assert compute_novelty(dedup_decision) == 1.0


def test_compute_novelty_matched() -> None:
    """AC #12: novelty == 0.0 when DedupDecision.is_new_cluster is False.

    This covers tier-1/2/3 match suppression.
    """
    dedup_decision = SimpleNamespace(is_new_cluster=False)
    assert compute_novelty(dedup_decision) == 0.0


def test_compute_novelty_none_sentinel() -> None:
    """AC #12 (implicit): compute_novelty(None) must return 1.0.

    The pre-classification / empty-store predictive path passes None because
    no cascade decision exists yet — the scorer must treat that as "new".
    """
    assert compute_novelty(None) == 1.0
