"""Priority-scoring signal primitives (AC #7-#12).

Each function computes a single scalar signal for a ``CandidateFinding``.
``compute_impact`` additionally returns the raw fan-in count and the
denominator's symbol count so the caller can record them in the
``PriorityScore.breakdown`` intermediates required by AC #6.

The impact signal is:

    normalized = log2(1 + n_callers) / log2(1 + graph.symbol_count)

with ``n_callers = |CallGraph.callers_of([symbol_id], max_depth=2)|``.
Absent symbols short-circuit to ``0.0`` (AC #8) and an empty graph is
similarly safe (no ``ZeroDivisionError``).

``compute_freshness`` and ``compute_owner_risk`` are deliberate stubs
returning ``0.5`` until git-blame/git-log integration lands;
``compute_confidence`` is a pass-through of the analyzer-supplied
confidence, and ``compute_novelty`` duck-types a ``DedupDecision``-like
input since ``DedupCascade`` is authored in a later segment.
"""

from __future__ import annotations

import math
from typing import Any, NamedTuple

from autofix_next.evidence.schema import CandidateFinding


class ImpactRaw(NamedTuple):
    """Triple returned by :func:`compute_impact`.

    ``normalized`` is the final 0..1 signal value used by the scorer;
    ``raw_callers_count`` and ``symbol_count`` are preserved for the
    breakdown intermediates that AC #6 mandates.
    """

    normalized: float
    raw_callers_count: int
    symbol_count: int


def compute_impact(finding: CandidateFinding, graph: Any) -> ImpactRaw:
    """Return the normalized impact signal for ``finding`` on ``graph``.

    Implements AC #7 and AC #8. The symbol id convention matches
    ``autofix_next.indexing.call_graph``: ``"<relpath>::<qualified-name>"``.
    If the symbol is absent from the graph, or the graph is empty, we
    return a zero-valued triple rather than raising — this is the "no
    ZeroDivision, no exception" guarantee from AC #8.
    """
    symbol_count = int(graph.symbol_count)
    if symbol_count <= 0:
        return ImpactRaw(0.0, 0, symbol_count if symbol_count > 0 else 0)

    denom = math.log2(1 + symbol_count)
    if denom == 0.0:
        return ImpactRaw(0.0, 0, symbol_count)

    sid = f"{finding.path}::{finding.symbol_name}"
    if sid not in graph.all_symbols:
        return ImpactRaw(0.0, 0, symbol_count)

    callers = graph.callers_of([sid], max_depth=2)
    n = len(callers)
    normalized = math.log2(1 + n) / denom
    return ImpactRaw(normalized, n, symbol_count)


def compute_freshness(finding: CandidateFinding) -> float:
    """AC #9: stub value ``0.5`` until git-blame freshness lands."""
    del finding  # stub — signature preserved for downstream swap-in.
    return 0.5


def compute_owner_risk(finding: CandidateFinding) -> float:
    """AC #10: stub value ``0.5`` until git-log ownership risk lands."""
    del finding  # stub — signature preserved for downstream swap-in.
    return 0.5


def compute_confidence(finding: CandidateFinding) -> float:
    """AC #11: pass-through of ``finding.analyzer_confidence``."""
    return finding.analyzer_confidence


def compute_novelty(dedup_decision: Any) -> float:
    """AC #12: novelty signal derived from the dedup decision.

    ``None`` means the cluster store was empty at scan start and the
    caller is pre-classifying; in that case the finding is guaranteed to
    mint a new cluster so we return ``1.0``. Otherwise we duck-type
    ``.is_new_cluster``: truthy ⇒ ``1.0`` (newly minted cluster),
    falsy ⇒ ``0.0`` (tier-1/2/3 match suppressed the finding).
    """
    if dedup_decision is None:
        return 1.0
    return 1.0 if getattr(dedup_decision, "is_new_cluster", False) else 0.0


__all__ = [
    "ImpactRaw",
    "compute_impact",
    "compute_freshness",
    "compute_owner_risk",
    "compute_confidence",
    "compute_novelty",
]
