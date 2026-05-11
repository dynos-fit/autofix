"""Unit tests for ``autofix.dedup.cluster_store``.

Covers AC #20 (atomic-rename), #21 (flock fallback), #22 (lock-free read),
#23 (persisted JSON shape), #34 (register_new_cluster lifecycle),
and the implicit missing-file load-returns-empty contract.
"""
from __future__ import annotations

import json
from pathlib import Path

from autofix.dedup.cluster_store import (
    CACHE_MODE_FALLBACK,
    CLUSTERS_FILENAME,
    SCHEMA_VERSION,
    STATE_DIRNAME,
    Cluster,
    ClusterStore,
)
from autofix.evidence.schema import CandidateFinding


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
    """AC #20, #22, #23: save then load must faithfully reproduce cluster state."""
    store = ClusterStore()
    f1 = _make_finding(finding_id="fp_one")
    f2 = _make_finding(finding_id="fp_two", path="pkg/other.py")
    store.register_new_cluster(f1, simhash=0x1111)
    store.register_new_cluster(f2, simhash=0x2222)

    store.save(tmp_path)

    reloaded = ClusterStore.load(tmp_path)

    assert reloaded.cluster_count == 2
    assert reloaded.find_by_fingerprint("fp_one") is not None
    assert reloaded.find_by_fingerprint("fp_two") is not None


def test_persisted_json_shape(tmp_path: Path) -> None:
    """AC #23: on-disk JSON has exactly these 3 top-level keys.

    Keys must be: schema_version, built_at, clusters. schema_version
    value must equal 'clusters_v1'.
    """
    store = ClusterStore()
    finding = _make_finding(finding_id="fp_shape")
    store.register_new_cluster(finding, simhash=0x33)
    store.save(tmp_path)

    json_path = tmp_path / STATE_DIRNAME / CLUSTERS_FILENAME
    assert json_path.is_file()
    data = json.loads(json_path.read_text(encoding="utf-8"))

    assert set(data.keys()) == {"schema_version", "built_at", "clusters"}
    assert data["schema_version"] == SCHEMA_VERSION
    assert SCHEMA_VERSION == "clusters_v1"


def test_atomic_rename_behavior(tmp_path: Path, monkeypatch) -> None:
    """AC #20: a failure during os.replace must NOT leave a corrupt final file."""
    import autofix.dedup.cluster_store as cs_mod

    store = ClusterStore()
    finding = _make_finding(finding_id="fp_atomic")
    store.register_new_cluster(finding, simhash=0x44)

    def _raise(src, dst):
        raise RuntimeError("simulated os.replace failure")

    monkeypatch.setattr(cs_mod.os, "replace", _raise)

    final_path = tmp_path / STATE_DIRNAME / CLUSTERS_FILENAME
    try:
        store.save(tmp_path)
    except RuntimeError:
        pass

    assert not final_path.is_file()


def test_blocking_io_error_fallback(tmp_path: Path, monkeypatch) -> None:
    """AC #21: a persistent BlockingIOError must set last_cache_mode fallback."""
    from contextlib import contextmanager

    @contextmanager
    def _raise_blocking(self, lock_path):
        raise BlockingIOError("simulated flock timeout")
        yield  # pragma: no cover - unreachable

    monkeypatch.setattr(ClusterStore, "_acquire_lock", _raise_blocking)

    store = ClusterStore()
    finding = _make_finding(finding_id="fp_block")
    store.register_new_cluster(finding, simhash=0x55)

    store.save(tmp_path)

    assert store.last_cache_mode == CACHE_MODE_FALLBACK
    assert CACHE_MODE_FALLBACK == "fallback_concurrent_writer"


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    """Implicit-req: ClusterStore.load on a fresh dir returns an empty store."""
    reloaded = ClusterStore.load(tmp_path)
    assert reloaded.is_empty is True
    assert reloaded.cluster_count == 0


def test_load_lock_free(tmp_path: Path, monkeypatch) -> None:
    """AC #22: load never calls fcntl.flock — readers are strictly lock-free."""
    import autofix.dedup.cluster_store as cs_mod

    seed_store = ClusterStore()
    finding = _make_finding(finding_id="fp_read")
    seed_store.register_new_cluster(finding, simhash=0x66)
    seed_store.save(tmp_path)

    flock_calls: list[tuple] = []

    def _no_flock(fd, op):
        flock_calls.append((fd, op))
        raise AssertionError("load must not call fcntl.flock")

    monkeypatch.setattr(cs_mod.fcntl, "flock", _no_flock)

    reloaded = ClusterStore.load(tmp_path)

    assert reloaded.cluster_count == 1
    assert flock_calls == []


def test_register_new_cluster_lifecycle() -> None:
    """AC #34: register_new_cluster sets occurrence_count=1, first_seen==last_seen."""
    store = ClusterStore()
    finding = _make_finding(finding_id="fp_life")

    cid = store.register_new_cluster(finding, simhash=0x77)

    cluster = store.find_by_fingerprint("fp_life")
    assert cluster is not None
    assert cluster.cluster_id == cid
    assert cluster.cluster_id.startswith("cl_")
    assert cluster.occurrence_count == 1
    assert cluster.first_seen == cluster.last_seen
    assert "T" in cluster.first_seen


def test_update_on_match_increments_and_advances_last_seen() -> None:
    """AC #34: update_on_match increments occurrence_count and advances last_seen."""
    import time

    store = ClusterStore()
    finding_a = _make_finding(finding_id="fp_life_a")
    store.register_new_cluster(finding_a, simhash=0x88)

    cluster = store.find_by_fingerprint("fp_life_a")
    assert cluster is not None
    original_first_seen = cluster.first_seen

    time.sleep(0.002)

    finding_b = _make_finding(finding_id="fp_life_b")
    store.update_on_match(cluster, finding_b, simhash=0x88)

    assert cluster.occurrence_count == 2
    assert cluster.first_seen == original_first_seen
    assert cluster.last_seen >= cluster.first_seen


def test_cluster_record_is_dataclass() -> None:
    """Sanity check: Cluster is a dataclass with the expected fields."""
    from dataclasses import fields, is_dataclass

    assert is_dataclass(Cluster)
    names = {f.name for f in fields(Cluster)}
    assert {
        "cluster_id",
        "canonical_fingerprint",
        "member_fingerprints",
        "simhash_signature",
        "first_seen",
        "last_seen",
        "occurrence_count",
    }.issubset(names)
