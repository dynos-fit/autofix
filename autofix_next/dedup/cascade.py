"""Three-tier dedup cascade.

Tier 1: exact fingerprint match (reuses finding.finding_id which was
        computed via evidence.fingerprints.compute_finding_fingerprint
        upstream -- cascade never re-hashes).
Tier 2: SimHash near-duplicate (Hamming <= 3).
Tier 3: embedding cosine similarity >= 0.85 (optional -- skipped when
        store.embedding_tier_available is False).

The cascade is first-match-wins with strict tier1 -> tier2 -> tier3
ordering and early exit. No telemetry is emitted from here -- the
pipeline (seg-6) emits PriorityScored / FindingDeduped /
DedupEmbeddingTierStatus / ClusterStorePersisted envelopes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from autofix_next.evidence.schema import CandidateFinding
from autofix_next.dedup.simhash import compute_simhash
from autofix_next.dedup.cluster_store import ClusterStore
from autofix_next.ranking.priority_scorer import PriorityScore


@dataclass(slots=True, frozen=True)
class DedupDecision:
    """Outcome of a single cascade classification.

    cluster_id: the cluster the finding was assigned to (new or existing).
    tier: 0 when a new cluster was created, else 1/2/3 for the winning tier.
    novelty: 1.0 for new cluster, 0.0 for any tier match.
    is_new_cluster: mirror of (tier == 0) for callers that prefer a bool.
    """
    cluster_id: str
    tier: int
    novelty: float
    is_new_cluster: bool


class DedupCascade:
    """Three-tier first-match-wins cascade."""

    # Thresholds pinned in spec.md AC #17, #18.
    SIMHASH_MAX_HAMMING: int = 3
    EMBEDDING_MIN_SIMILARITY: float = 0.85

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
        # ----- Tier 1: exact fingerprint --------------------------------
        existing = store.find_by_fingerprint(finding.finding_id)
        if existing is not None:
            return DedupDecision(
                cluster_id=existing.cluster_id,
                tier=1,
                novelty=0.0,
                is_new_cluster=False,
            )

        # Compute SimHash once -- used by tier 2 and, on no-match, by
        # register_new_cluster.
        simhash = compute_simhash(finding, parse_result)

        # ----- Tier 2: SimHash Hamming <= 3 -----------------------------
        sim_match = store.find_by_simhash(simhash, max_hamming=self.SIMHASH_MAX_HAMMING)
        if sim_match is not None:
            # Update cluster state (centroid stays whatever it was, since
            # we did not compute an embedding on this tier -- AC #35's
            # (old*n+new)/(n+1) only applies when a new embedding is
            # present).
            store.update_on_match(sim_match, finding, simhash, None)
            return DedupDecision(
                cluster_id=sim_match.cluster_id,
                tier=2,
                novelty=0.0,
                is_new_cluster=False,
            )

        # ----- Tier 3: embedding cosine >= 0.85 (optional) --------------
        embedding_vec: list[float] | None = None
        if store.embedding_tier_available:
            try:
                from autofix_next.dedup.embedding import embed_text
                embedding_vec = embed_text(
                    finding.changed_slice + " " + finding.rule_id
                )
            except Exception:
                # Defensive: if embedding fails at call time despite the
                # probe saying available, degrade silently to tier-2-only.
                embedding_vec = None

            if embedding_vec is not None:
                emb_match = store.find_by_embedding(
                    embedding_vec,
                    min_similarity=self.EMBEDDING_MIN_SIMILARITY,
                )
                if emb_match is not None:
                    store.update_on_match(emb_match, finding, simhash, embedding_vec)
                    return DedupDecision(
                        cluster_id=emb_match.cluster_id,
                        tier=3,
                        novelty=0.0,
                        is_new_cluster=False,
                    )

        # ----- No match: register a new cluster -------------------------
        new_id = store.register_new_cluster(finding, simhash, embedding_vec)
        return DedupDecision(
            cluster_id=new_id,
            tier=0,
            novelty=1.0,
            is_new_cluster=True,
        )


__all__ = ["DedupCascade", "DedupDecision"]
