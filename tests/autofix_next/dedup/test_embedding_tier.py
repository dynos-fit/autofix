"""Tier-3 embedding behavior.

These tests are marked ``requires_dedup_extras`` and are auto-skipped
by conftest.py when the optional extras are missing.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.requires_dedup_extras


def test_cosine_match_above_threshold():
    from autofix_next.dedup.embedding import embed_text, cosine_similarity

    a = embed_text("unused import intra file: os")
    b = embed_text("unused import intra file: os")
    assert cosine_similarity(a, b) >= 0.85


def test_hnswlib_add_and_search():
    from autofix_next.dedup.embedding import HNSWIndex, embed_text

    idx = HNSWIndex()
    vecs = [
        embed_text("unused import intra file: os"),
        embed_text("unused import intra file: sys"),
        embed_text("unused import intra file: json"),
    ]
    cids = ["cl_aaa", "cl_bbb", "cl_ccc"]
    idx.add_items(vecs, cids)

    # Query with near-identical text to the first vector; nearest
    # should be cl_aaa.
    hits = idx.search(embed_text("unused import intra file: os"), k=1)
    assert hits
    best_cid, _best_dist = hits[0]
    assert best_cid == "cl_aaa"
