"""Two-tier dedup cascade.

Tier 1: exact fingerprint match (reuses finding.finding_id which was
        computed via evidence.fingerprints.compute_finding_fingerprint
        upstream -- cascade never re-hashes).
Tier 2: SimHash near-duplicate (Hamming <= 3).

The cascade is first-match-wins with strict tier1 -> tier2 ordering
and early exit. No telemetry is emitted from here -- the pipeline
emits PriorityScored / FindingDeduped / ClusterStorePersisted envelopes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opentelemetry.trace import get_current_span

from autofix.evidence.schema import CandidateFinding, PriorityScore
from autofix.dedup.simhash import compute_simhash
from autofix.dedup.cluster_store import ClusterStore
from autofix.telemetry.correlation import current_commit_sha, current_scan_id
from autofix.telemetry.tracer import span


@dataclass(slots=True, frozen=True)
class DedupDecision:
    """Outcome of a single cascade classification.

    cluster_id: the cluster the finding was assigned to (new or existing).
    tier: 0 when a new cluster was created, else 1/2 for the winning tier.
    novelty: 1.0 for new cluster, 0.0 for any tier match.
    is_new_cluster: mirror of (tier == 0) for callers that prefer a bool.
    """
    cluster_id: str
    tier: int
    novelty: float
    is_new_cluster: bool


class DedupCascade:
    """Two-tier first-match-wins cascade."""

    SIMHASH_MAX_HAMMING: int = 3

    def classify(
        self,
        finding: CandidateFinding,
        score: PriorityScore,
        store: ClusterStore,
        parse_result: Any = None,
    ) -> DedupDecision:
        """Run the cascade. First-match-wins with early exit.

        ``score`` is kept as a parameter per spec.md AC #15 signature even
        though cascade does not currently consume it -- future work may
        use score to rank canonical members inside a matched cluster.
        ``parse_result`` threads the already-parsed tree-sitter tree to
        compute_simhash without re-parsing.
        """
        with span(
            "autofix.dedup",
            scan_id=current_scan_id(),
            commit_sha=current_commit_sha(),
        ):
            # ----- Tier 1: exact fingerprint --------------------------------
            existing = store.find_by_fingerprint(finding.finding_id)
            if existing is not None:
                decision = DedupDecision(
                    cluster_id=existing.cluster_id,
                    tier=1,
                    novelty=0.0,
                    is_new_cluster=False,
                )
            else:
                # Compute SimHash once -- used by tier 2 and, on no-match, by
                # register_new_cluster.
                simhash = compute_simhash(finding, parse_result)

                # ----- Tier 2: SimHash Hamming <= 3 -----------------------------
                sim_match = store.find_by_simhash(
                    simhash, max_hamming=self.SIMHASH_MAX_HAMMING
                )
                if sim_match is not None:
                    store.update_on_match(sim_match, finding, simhash)
                    decision = DedupDecision(
                        cluster_id=sim_match.cluster_id,
                        tier=2,
                        novelty=0.0,
                        is_new_cluster=False,
                    )
                else:
                    new_id = store.register_new_cluster(finding, simhash)
                    decision = DedupDecision(
                        cluster_id=new_id,
                        tier=0,
                        novelty=1.0,
                        is_new_cluster=True,
                    )

            s = get_current_span()
            s.set_attribute("cluster_count", int(store.cluster_count))
            s.set_attribute("tier_matched", int(decision.tier))
            return decision


__all__ = ["DedupCascade", "DedupDecision"]
