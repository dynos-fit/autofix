"""Composite priority scorer (AC #4, #5, #6).

``PriorityScorer.score`` folds the five signals from
:mod:`autofix_next.ranking.signals` into a single deterministic scalar
using the fixed weights listed in AC #5::

    priority = 0.35 * impact
             + 0.25 * freshness
             + 0.20 * confidence
             + 0.10 * novelty
             + 0.10 * owner_risk

The returned :class:`PriorityScore` is a frozen, slotted dataclass whose
``breakdown`` dict carries both the five principal signals and the three
intermediates required by AC #6. ``novelty_cluster_match_state`` is
serialized as a float (``1.0`` ⇒ new cluster, ``0.0`` ⇒ tier-1 match
predicted) so the dict can stay uniformly ``dict[str, float]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from autofix_next.evidence.schema import CandidateFinding
from autofix_next.ranking.signals import (
    compute_confidence,
    compute_freshness,
    compute_impact,
    compute_owner_risk,
)


@dataclass(slots=True, frozen=True)
class PriorityScore:
    """AC #4: immutable record of a single finding's composite priority.

    ``breakdown`` is required (per AC #6) to contain at minimum::

        impact, freshness, confidence, novelty, owner_risk,
        impact_raw_callers_count, impact_symbol_count,
        novelty_cluster_match_state
    """

    finding_id: str
    priority: float
    breakdown: dict[str, float]


class PriorityScorer:
    """Weighted composite priority scorer (AC #5).

    Weights are declared as class-level floats so callers may introspect
    them (e.g. for explainability UIs) without instantiating the scorer.
    """

    W_IMPACT: float = 0.35
    W_FRESHNESS: float = 0.25
    W_CONFIDENCE: float = 0.20
    W_NOVELTY: float = 0.10
    W_OWNER_RISK: float = 0.10

    def score(
        self,
        finding: CandidateFinding,
        graph: Any,
        cluster_store: Any,
    ) -> PriorityScore:
        """Return the :class:`PriorityScore` for ``finding``.

        The scorer duck-types ``cluster_store`` via ``is_empty`` and
        ``find_by_fingerprint`` because the concrete ``ClusterStore``
        implementation lands in seg-4. Predicted novelty resolves as
        follows:

        * ``cluster_store.is_empty`` truthy → ``1.0`` (first finding of
          the scan; guaranteed new cluster).
        * fingerprint not in store → ``1.0`` (no tier-1 match; cascade
          may still reclassify downstream, but the scorer commits
          deterministically to 1.0 for replay stability).
        * fingerprint present in store → ``0.0`` (tier-1 match).
        """
        impact_raw = compute_impact(finding, graph)
        freshness = compute_freshness(finding)
        confidence = compute_confidence(finding)
        owner_risk = compute_owner_risk(finding)

        novelty: float
        novelty_state: float
        if getattr(cluster_store, "is_empty", True):
            novelty = 1.0
            novelty_state = 1.0
        else:
            existing = cluster_store.find_by_fingerprint(finding.finding_id)
            if existing is None:
                novelty = 1.0
                novelty_state = 1.0
            else:
                novelty = 0.0
                novelty_state = 0.0

        priority = (
            self.W_IMPACT * impact_raw.normalized
            + self.W_FRESHNESS * freshness
            + self.W_CONFIDENCE * confidence
            + self.W_NOVELTY * novelty
            + self.W_OWNER_RISK * owner_risk
        )

        breakdown: dict[str, float] = {
            "impact": impact_raw.normalized,
            "freshness": freshness,
            "confidence": confidence,
            "novelty": novelty,
            "owner_risk": owner_risk,
            "impact_raw_callers_count": float(impact_raw.raw_callers_count),
            "impact_symbol_count": float(impact_raw.symbol_count),
            "novelty_cluster_match_state": novelty_state,
        }

        return PriorityScore(
            finding_id=finding.finding_id,
            priority=priority,
            breakdown=breakdown,
        )


__all__ = ["PriorityScore", "PriorityScorer"]
