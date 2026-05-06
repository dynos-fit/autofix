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

from opentelemetry.trace import get_current_span

from autofix.evidence.schema import CandidateFinding
from autofix.dedup.simhash import compute_simhash
from autofix.dedup.cluster_store import ClusterStore
from autofix.ranking.priority_scorer import PriorityScore
from autofix.telemetry.correlation import current_commit_sha, current_scan_id
from autofix.telemetry.tracer import span


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
        *,
        recall_hits: list | None = None,
    ) -> DedupDecision:
        """Run the cascade. First-match-wins with early exit.

        ``score`` is kept as a parameter per spec.md AC #15 signature even
        though cascade does not currently consume it -- future work may
        use score to rank canonical members inside a matched cluster.
        ``parse_result`` threads the already-parsed tree-sitter tree to
        compute_simhash without re-parsing.

        ``recall_hits`` (task-012 AC 15/16) is an optional keyword-only
        list of ``SymbolRecall``-like hits from the embedding sidecar.
        ``None`` and ``[]`` preserve byte-identical pre-task-012 behavior;
        a non-empty list is currently only attached to the cascade span
        as an observability signal and does not alter match decisions.
        """
        recall_count = len(recall_hits) if recall_hits else 0
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
                    # Update cluster state (centroid stays whatever it was, since
                    # we did not compute an embedding on this tier -- AC #35's
                    # (old*n+new)/(n+1) only applies when a new embedding is
                    # present).
                    store.update_on_match(sim_match, finding, simhash, None)
                    decision = DedupDecision(
                        cluster_id=sim_match.cluster_id,
                        tier=2,
                        novelty=0.0,
                        is_new_cluster=False,
                    )
                else:
                    # ----- Tier 3: embedding cosine >= 0.85 (optional) --------------
                    embedding_vec: list[float] | None = None
                    if store.embedding_tier_available:
                        try:
                            from autofix.dedup.embedding import embed_text
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
                                store.update_on_match(
                                    emb_match, finding, simhash, embedding_vec
                                )
                                decision = DedupDecision(
                                    cluster_id=emb_match.cluster_id,
                                    tier=3,
                                    novelty=0.0,
                                    is_new_cluster=False,
                                )
                            else:
                                # ----- No match: register a new cluster -------------------------
                                new_id = store.register_new_cluster(
                                    finding, simhash, embedding_vec
                                )
                                decision = DedupDecision(
                                    cluster_id=new_id,
                                    tier=0,
                                    novelty=1.0,
                                    is_new_cluster=True,
                                )
                        else:
                            # Embedding tier available but embed_text failed —
                            # register a new cluster.
                            new_id = store.register_new_cluster(
                                finding, simhash, embedding_vec
                            )
                            decision = DedupDecision(
                                cluster_id=new_id,
                                tier=0,
                                novelty=1.0,
                                is_new_cluster=True,
                            )
                    else:
                        # Embedding tier unavailable — register a new cluster.
                        new_id = store.register_new_cluster(
                            finding, simhash, embedding_vec
                        )
                        decision = DedupDecision(
                            cluster_id=new_id,
                            tier=0,
                            novelty=1.0,
                            is_new_cluster=True,
                        )

            s = get_current_span()
            s.set_attribute("cluster_count", int(store.cluster_count))
            s.set_attribute("tier_matched", int(decision.tier))
            if recall_count:
                s.set_attribute("recall_hit_count", int(recall_count))
            return decision


__all__ = ["DedupCascade", "DedupDecision"]
