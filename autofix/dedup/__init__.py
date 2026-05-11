"""Two-tier dedup subsystem: fingerprint / SimHash.

This subpackage hosts the dedup cascade that classifies a candidate
finding against previously-seen clusters in two escalating tiers:

1. Exact fingerprint match (seg-1 / evidence.fingerprints).
2. SimHash near-duplicate detection (this segment — seg-2).
"""

from autofix.dedup.simhash import (
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
