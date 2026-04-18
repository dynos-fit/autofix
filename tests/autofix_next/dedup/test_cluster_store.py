"""Unit tests for ``autofix_next.dedup.cluster_store``.

Covers AC #20 (atomic-rename), #21 (flock fallback), #22 (lock-free read),
#23 (persisted JSON shape), #24 (HNSW sidecar), #25 (embedding tier probe),
#34 (register_new_cluster lifecycle), #35 (incremental centroid formula),
and the implicit missing-file load-returns-empty contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autofix_next.dedup.cluster_store import (
    CACHE_MODE_FALLBACK,
    CLUSTERS_FILENAME,
    HNSW_FILENAME,
    SCHEMA_VERSION,
    STATE_DIRNAME,
    Cluster,
    ClusterStore,
)
from autofix_next.dedup.embedding import probe_embedding_tier
from autofix_next.evidence.schema import CandidateFinding


def _make_finding(
    *,
    rule_id: str = "unused-import",
    path: str = "pkg/mod.py",
    symbol_name: str = "my_func",
    normalized_import: str = "os",
    start_line: int = 10,
    end_line: int = 10,
    changed_slice: str = "import os",
    finding_id: str = "fp_default",
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
    )


def test_save_load_round_trip(tmp_path: Path) -> None:
    """AC #20, #22, #23: save then load must faithfully reproduce cluster state.

    Register two clusters, save, then load a fresh store from the same
    root. The reloaded store must contain both clusters and both fingerprint
    lookups must succeed.
    """
    store = ClusterStore()
    f1 = _make_finding(finding_id="fp_one")
    f2 = _make_finding(finding_id="fp_two", path="pkg/other.py")
    store.register_new_cluster(f1, simhash=0x1111, embedding=None)
    store.register_new_cluster(f2, simhash=0x2222, embedding=None)

    store.save(tmp_path)

    reloaded = ClusterStore.load(tmp_path)

    assert reloaded.cluster_count == 2
    assert reloaded.find_by_fingerprint("fp_one") is not None
    assert reloaded.find_by_fingerprint("fp_two") is not None


def test_persisted_json_shape(tmp_path: Path) -> None:
    """AC #23: on-disk JSON has exactly these 5 top-level keys.

    Keys must be: schema_version, built_at, embedding_tier_used,
    embedding_model, clusters. schema_version value must equal
    'clusters_v1'.
    """
    store = ClusterStore()
    finding = _make_finding(finding_id="fp_shape")
    store.register_new_cluster(finding, simhash=0x33, embedding=None)
    store.save(tmp_path)

    json_path = tmp_path / STATE_DIRNAME / CLUSTERS_FILENAME
    assert json_path.is_file()
    data = json.loads(json_path.read_text(encoding="utf-8"))

    assert set(data.keys()) == {
        "schema_version",
        "built_at",
        "embedding_tier_used",
        "embedding_model",
        "clusters",
    }
    assert data["schema_version"] == SCHEMA_VERSION
    assert SCHEMA_VERSION == "clusters_v1"


def test_atomic_rename_behavior(tmp_path: Path, monkeypatch) -> None:
    """AC #20: a failure during os.replace must NOT leave a corrupt final file.

    Monkeypatch os.replace inside the cluster_store module to raise. On
    an empty pre-state, the final clusters.json must NOT exist after save.
    """
    import autofix_next.dedup.cluster_store as cs_mod

    store = ClusterStore()
    finding = _make_finding(finding_id="fp_atomic")
    store.register_new_cluster(finding, simhash=0x44, embedding=None)

    def _raise(src, dst):
        raise RuntimeError("simulated os.replace failure")

    monkeypatch.setattr(cs_mod.os, "replace", _raise)

    # The current implementation does not catch RuntimeError from os.replace,
    # so save may propagate. What matters for AC #20 is the final file must
    # not exist.
    final_path = tmp_path / STATE_DIRNAME / CLUSTERS_FILENAME
    try:
        store.save(tmp_path)
    except RuntimeError:
        pass

    assert not final_path.is_file()


def test_blocking_io_error_fallback(tmp_path: Path, monkeypatch) -> None:
    """AC #21: a persistent BlockingIOError must set last_cache_mode fallback.

    Monkeypatch the ClusterStore._acquire_lock contextmanager on the class
    so it raises BlockingIOError on entry. save must not raise and must set
    last_cache_mode == 'fallback_concurrent_writer'.
    """
    from contextlib import contextmanager

    @contextmanager
    def _raise_blocking(self, lock_path):
        raise BlockingIOError("simulated flock timeout")
        yield  # pragma: no cover - unreachable

    monkeypatch.setattr(ClusterStore, "_acquire_lock", _raise_blocking)

    store = ClusterStore()
    finding = _make_finding(finding_id="fp_block")
    store.register_new_cluster(finding, simhash=0x55, embedding=None)

    # Must not raise.
    store.save(tmp_path)

    assert store.last_cache_mode == CACHE_MODE_FALLBACK
    assert CACHE_MODE_FALLBACK == "fallback_concurrent_writer"


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    """Implicit-req: ClusterStore.load on a fresh dir returns an empty store.

    The first-ever scan in a repo must be valid — no clusters.json yet.
    """
    reloaded = ClusterStore.load(tmp_path)
    assert reloaded.is_empty is True
    assert reloaded.cluster_count == 0


def test_load_lock_free(tmp_path: Path, monkeypatch) -> None:
    """AC #22: load never calls fcntl.flock — readers are strictly lock-free.

    We monkeypatch fcntl.flock to raise inside the cluster_store module so
    any accidental lock attempt during load would trip. Save first (using
    the real flock), then restore the spy BEFORE calling load.
    """
    import autofix_next.dedup.cluster_store as cs_mod

    # Seed the store with one cluster so load has data to parse.
    seed_store = ClusterStore()
    finding = _make_finding(finding_id="fp_read")
    seed_store.register_new_cluster(finding, simhash=0x66, embedding=None)
    seed_store.save(tmp_path)

    flock_calls: list[tuple] = []

    def _no_flock(fd, op):
        flock_calls.append((fd, op))
        raise AssertionError("load must not call fcntl.flock")

    monkeypatch.setattr(cs_mod.fcntl, "flock", _no_flock)

    reloaded = ClusterStore.load(tmp_path)

    assert reloaded.cluster_count == 1
    assert flock_calls == []


def test_embedding_tier_probe_handled() -> None:
    """AC #25: store.embedding_tier_available matches probe_embedding_tier().

    Whether the optional [dedup] extras are installed or not, the probe
    must return a (bool, reason) pair and the store's init must mirror
    the bool without raising.
    """
    available, reason = probe_embedding_tier()
    assert isinstance(available, bool)
    assert isinstance(reason, str)
    assert reason in (
        "available",
        "deps_missing",
        "model_cache_missing_offline",
    )

    store = ClusterStore()
    assert store.embedding_tier_available == available
    assert store.embedding_tier_reason == reason


def test_register_new_cluster_lifecycle() -> None:
    """AC #34: register_new_cluster sets occurrence_count=1, first_seen==last_seen.

    cluster_id must start with 'cl_'. Timestamps must be ISO-8601 UTC strings.
    """
    store = ClusterStore()
    finding = _make_finding(finding_id="fp_life")

    cid = store.register_new_cluster(finding, simhash=0x77, embedding=None)

    cluster = store.find_by_fingerprint("fp_life")
    assert cluster is not None
    assert cluster.cluster_id == cid
    assert cluster.cluster_id.startswith("cl_")
    assert cluster.occurrence_count == 1
    assert cluster.first_seen == cluster.last_seen
    # ISO-8601 UTC uses a T separator; sanity-check the format.
    assert "T" in cluster.first_seen


def test_update_on_match_increments_and_advances_last_seen() -> None:
    """AC #34: update_on_match increments occurrence_count and advances last_seen.

    first_seen must remain unchanged. last_seen must be >= first_seen in ISO
    ordering (string comparison works because ISO-8601 is lexicographically
    orderable).
    """
    import time

    store = ClusterStore()
    finding_a = _make_finding(finding_id="fp_life_a")
    store.register_new_cluster(finding_a, simhash=0x88, embedding=None)

    cluster = store.find_by_fingerprint("fp_life_a")
    assert cluster is not None
    original_first_seen = cluster.first_seen

    # Sleep just enough that the ISO-8601 microsecond field will differ.
    time.sleep(0.002)

    finding_b = _make_finding(finding_id="fp_life_b")
    store.update_on_match(cluster, finding_b, simhash=0x88, embedding=None)

    assert cluster.occurrence_count == 2
    assert cluster.first_seen == original_first_seen
    assert cluster.last_seen >= cluster.first_seen


def test_incremental_centroid_formula() -> None:
    """AC #35: new_centroid = (old_centroid * n + new_embedding) / (n + 1).

    Starting with a cluster whose centroid is [1.0, 1.0, 1.0] and n=1,
    updating with new embedding [4.0, 4.0, 4.0] yields:
        [(1*1 + 4)/2, (1*1 + 4)/2, (1*1 + 4)/2] = [2.5, 2.5, 2.5]
    """
    store = ClusterStore()
    finding_a = _make_finding(finding_id="fp_centroid_a")
    store.register_new_cluster(
        finding_a, simhash=0x99, embedding=[1.0, 1.0, 1.0]
    )

    cluster = store.find_by_fingerprint("fp_centroid_a")
    assert cluster is not None
    assert cluster.occurrence_count == 1
    assert cluster.embedding_centroid == [1.0, 1.0, 1.0]

    finding_b = _make_finding(finding_id="fp_centroid_b")
    store.update_on_match(
        cluster, finding_b, simhash=0x99, embedding=[4.0, 4.0, 4.0]
    )

    assert cluster.embedding_centroid is not None
    for actual, expected in zip(cluster.embedding_centroid, [2.5, 2.5, 2.5]):
        assert abs(actual - expected) < 1e-9


def test_hnsw_sidecar_written_when_tier3_active(tmp_path: Path) -> None:
    """AC #24: when the embedding tier is active, save writes clusters.hnswlib.idx.

    Skipped on machines without the [dedup] optional extras (the probe
    returns (False, 'deps_missing') and the tier-3 ANN cache is not created).
    """
    available, _ = probe_embedding_tier()
    if not available:
        pytest.skip("embedding tier unavailable — skipping AC #24 sidecar check")

    import hnswlib  # noqa: F401 - ensures the optional dep is importable

    from autofix_next.dedup.embedding import EMBEDDING_DIM, HNSWIndex

    store = ClusterStore()
    # Build a centroid of the correct dimensionality.
    centroid = [0.01] * EMBEDDING_DIM
    finding = _make_finding(finding_id="fp_hnsw")
    store.register_new_cluster(finding, simhash=0xAA, embedding=centroid)
    # Prime the in-memory HNSW index so save knows to emit the sidecar.
    idx = HNSWIndex(dim=EMBEDDING_DIM, max_elements=16)
    idx.add_items([centroid], ["cl_hnsw_seed"])
    store._hnsw = idx  # exercise the saved-sidecar branch

    store.save(tmp_path)

    sidecar = tmp_path / STATE_DIRNAME / HNSW_FILENAME
    assert sidecar.is_file()


def test_cluster_record_is_dataclass() -> None:
    """Sanity check: Cluster is a dataclass with the expected fields.

    Supports AC #34 / #35 by ensuring the record carries occurrence_count,
    first_seen, last_seen, and embedding_centroid.
    """
    from dataclasses import fields, is_dataclass

    assert is_dataclass(Cluster)
    names = {f.name for f in fields(Cluster)}
    assert {
        "cluster_id",
        "canonical_fingerprint",
        "member_fingerprints",
        "simhash_signature",
        "embedding_centroid",
        "first_seen",
        "last_seen",
        "occurrence_count",
    }.issubset(names)
