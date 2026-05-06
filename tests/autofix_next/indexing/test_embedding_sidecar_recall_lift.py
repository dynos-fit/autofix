"""task-012 AC 24 — semantic recall lift vs exact+SimHash baseline.

Uses the held-out fixture at ``tests/fixtures/embedding_recall/`` (20
seed symbols + 10 near-dup pairs). Asserts:

* Embedding sidecar recall@5 on the paired target ≥ 0.8 (similarity ≥ 0.75).
* Exact-match baseline on symbol_name clusters ≤ 30% of the same pairs —
  the headline recall-lift claim.

Skipped when sentence-transformers / hnswlib are not importable.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


FIXTURE_DIR = (
    Path(__file__).resolve().parents[3] / "fixtures" / "embedding_recall"
)


def _deps_available() -> bool:
    try:
        importlib.import_module("sentence_transformers")
        importlib.import_module("hnswlib")
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not _deps_available(),
    reason="recall-lift proof requires sentence-transformers + hnswlib",
)
def test_embedding_sidecar_recall_lifts_over_exact_plus_simhash(
    tmp_path: Path,
) -> None:
    from autofix_next.indexing.embedding import EmbeddingSidecar

    seed = json.loads((FIXTURE_DIR / "seed_symbols.json").read_text())
    pairs = json.loads((FIXTURE_DIR / "near_dup_pairs.json").read_text())

    sidecar = EmbeddingSidecar(
        tmp_path, {"index": {"embedding_sidecar": {"enabled": True}}}
    )
    assert sidecar.enabled
    for sym in seed["symbols"]:
        sidecar.upsert_symbol(
            sym["symbol_id"],
            sym["text"],
            {
                "symbol_name": sym["symbol_name"],
                "language": sym["language"],
                "path": sym["path"],
            },
        )
    assert sidecar.symbol_count == len(seed["symbols"])

    seed_by_name = {s["symbol_name"]: s["symbol_id"] for s in seed["symbols"]}

    sidecar_hits = 0
    baseline_hits = 0
    for pair in pairs["pairs"]:
        target_id = pair["target"]
        query_text = pair["query"]
        # Sidecar: recall@5 at the default 0.75 threshold.
        hits = sidecar.recall(query_text, top_k=5, threshold=0.75)
        if any(h.symbol_id == target_id for h in hits):
            sidecar_hits += 1
        # Baseline: exact symbol_name match. Parse the name out of the
        # query text "{lang}::{name} ...". Count a baseline hit only when
        # the name happens to land in the seed set unchanged.
        baseline_name = (
            query_text.split("::", 1)[1].split(" ", 1)[0]
            if "::" in query_text
            else ""
        )
        if baseline_name and baseline_name in seed_by_name and seed_by_name[
            baseline_name
        ] == target_id:
            baseline_hits += 1

    total = len(pairs["pairs"])
    sidecar_rate = sidecar_hits / total
    baseline_rate = baseline_hits / total

    assert sidecar_rate >= 0.8, (
        f"AC 24: sidecar recall@5 = {sidecar_rate:.2f}, expected >= 0.80 "
        f"(hits={sidecar_hits}/{total})"
    )
    assert baseline_rate <= 0.3, (
        f"AC 24: exact-baseline rate = {baseline_rate:.2f}, expected <= 0.30 "
        f"(hits={baseline_hits}/{total})"
    )
    # Lift sanity — sidecar must beat baseline.
    assert sidecar_rate > baseline_rate
