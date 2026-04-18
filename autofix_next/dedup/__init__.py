"""Three-tier dedup subsystem: fingerprint / SimHash / embedding.

This subpackage hosts the dedup cascade that classifies a candidate
finding against previously-seen clusters in three escalating tiers:

1. Exact fingerprint match (seg-1 / evidence.fingerprints).
2. SimHash near-duplicate detection (this segment — seg-2).
3. Embedding-based semantic neighbor search (seg-3, optional extras).

The public surface re-exported below belongs to the SimHash tier owned
by seg-2. Later segments (seg-4 cluster store, seg-5 dedup cascade)
will EXTEND this file append-only with their own re-exports; downstream
consumers import names directly from :mod:`autofix_next.dedup` so the
internal module layout can evolve without breaking callers.
"""

from autofix_next.dedup.simhash import (
    ast_node_type_path,
    compute_simhash,
    hamming_distance,
    path_components,
    tokenize_rule_id,
)

__all__ = [
    "compute_simhash",
    "hamming_distance",
    "tokenize_rule_id",
    "path_components",
    "ast_node_type_path",
]
