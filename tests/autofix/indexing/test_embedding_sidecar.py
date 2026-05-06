"""Unit tests for the :mod:`autofix.indexing.embedding` sidecar.

Covers task-012 AC 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13.

Live HNSW/embedding calls are gated on the optional deps being present.
When sentence-transformers or hnswlib is not importable, only the
disabled / degraded / probe contracts are exercised — they are the
paths that must always work.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from autofix.indexing.embedding import (
    EMBEDDING_SIDECAR_DIM,
    EMBEDDING_SIDECAR_MODEL_NAME,
    EmbeddingSidecar,
    SymbolRecall,
    probe_embedding_sidecar,
)


def _deps_available() -> bool:
    try:
        importlib.import_module("sentence_transformers")
        importlib.import_module("hnswlib")
    except ImportError:
        return False
    return True


HAVE_DEPS = _deps_available()


# ---------------------------------------------------------------------------
# AC 1 — public surface
# ---------------------------------------------------------------------------


def test_public_surface_is_exactly_five_names() -> None:
    import autofix.indexing.embedding as mod

    assert set(mod.__all__) == {
        "EMBEDDING_SIDECAR_MODEL_NAME",
        "EMBEDDING_SIDECAR_DIM",
        "SymbolRecall",
        "EmbeddingSidecar",
        "probe_embedding_sidecar",
    }
    assert mod.EMBEDDING_SIDECAR_MODEL_NAME == "all-MiniLM-L6-v2"
    assert mod.EMBEDDING_SIDECAR_DIM == 384


# ---------------------------------------------------------------------------
# AC 2 — SymbolRecall shape
# ---------------------------------------------------------------------------


def test_symbolrecall_is_frozen_slotted_dataclass() -> None:
    assert SymbolRecall.__dataclass_params__.frozen is True

    fields = list(SymbolRecall.__dataclass_fields__.keys())
    assert fields == [
        "symbol_id",
        "similarity",
        "symbol_name",
        "language",
        "path",
    ]

    # Slots — instances must reject attribute assignment and carry no
    # __dict__ overhead.
    sr = SymbolRecall(
        symbol_id="s1",
        similarity=0.9,
        symbol_name="parse",
        language="python",
        path="a.py",
    )
    assert not hasattr(sr, "__dict__")
    with pytest.raises(AttributeError):
        sr.symbol_id = "s2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AC 3 / AC 4 — __init__ / .enabled
# ---------------------------------------------------------------------------


def test_init_with_none_policy_is_disabled(tmp_path: Path) -> None:
    sc = EmbeddingSidecar(tmp_path, None)
    assert sc.enabled is False


def test_init_with_missing_policy_keys_is_disabled(tmp_path: Path) -> None:
    sc = EmbeddingSidecar(tmp_path, {"unrelated": {"flag": True}})
    assert sc.enabled is False


def test_init_with_enabled_false_is_disabled(tmp_path: Path) -> None:
    sc = EmbeddingSidecar(
        tmp_path, {"index": {"embedding_sidecar": {"enabled": False}}}
    )
    assert sc.enabled is False


# ---------------------------------------------------------------------------
# AC 5 — disabled path is total no-op
# ---------------------------------------------------------------------------


def test_disabled_methods_are_noops(tmp_path: Path) -> None:
    sc = EmbeddingSidecar(tmp_path, None)
    assert sc.upsert_symbol("s", "text", {}) is None
    assert sc.recall("q") == []
    assert sc.save() is False
    assert sc.load() is False
    assert sc.cold_rebuild([{"symbol_id": "s", "text": "t"}]) == 0
    assert sc.symbol_count == 0
    # No ML model load, no disk I/O on the sidecar dir.
    assert not (tmp_path / ".autofix" / "indexing").exists()


# ---------------------------------------------------------------------------
# AC 6 — enabled-but-deps-missing path emits EmbeddingSidecarDegraded
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    HAVE_DEPS,
    reason="deps present — degraded path only fires when imports fail",
)
def test_enabled_with_missing_deps_emits_degraded_event(
    tmp_path: Path,
) -> None:
    sc = EmbeddingSidecar(
        tmp_path, {"index": {"embedding_sidecar": {"enabled": True}}}
    )
    assert sc.enabled is False  # self-disabled after catching ImportError
    events_path = tmp_path / ".autofix" / "events.jsonl"
    assert events_path.exists()
    rows = [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line
    ]
    degraded = [r for r in rows if r.get("event") == "EmbeddingSidecarDegraded"]
    assert len(degraded) == 1
    payload = degraded[0]["scan_event"]
    assert payload["event_type"] == "EmbeddingSidecarDegraded"
    assert "reason" in payload


# ---------------------------------------------------------------------------
# AC 13 — probe does NOT instantiate the class
# ---------------------------------------------------------------------------


def test_probe_three_return_cases() -> None:
    # disabled by policy
    assert probe_embedding_sidecar(None) == (False, "disabled_by_policy")
    assert probe_embedding_sidecar({}) == (False, "disabled_by_policy")
    assert probe_embedding_sidecar(
        {"index": {"embedding_sidecar": {"enabled": False}}}
    ) == (False, "disabled_by_policy")

    enabled_policy = {"index": {"embedding_sidecar": {"enabled": True}}}
    ok, reason = probe_embedding_sidecar(enabled_policy)
    if HAVE_DEPS:
        assert (ok, reason) == (True, "ok")
    else:
        assert (ok, reason) == (False, "deps_missing")


# ---------------------------------------------------------------------------
# AC 7 / 8 — upsert + recall roundtrip (needs deps)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not HAVE_DEPS, reason="upsert/recall requires sentence-transformers + hnswlib"
)
def test_upsert_and_recall_roundtrip(tmp_path: Path) -> None:
    sc = EmbeddingSidecar(
        tmp_path, {"index": {"embedding_sidecar": {"enabled": True}}}
    )
    assert sc.enabled is True
    sc.upsert_symbol(
        "sym_a",
        "python::parse_json def parse_json(p): return json.load(open(p))",
        {"symbol_name": "parse_json", "language": "python", "path": "a.py"},
    )
    sc.upsert_symbol(
        "sym_b",
        "python::compute_md5 def compute_md5(d): return md5(d).hexdigest()",
        {"symbol_name": "compute_md5", "language": "python", "path": "b.py"},
    )
    assert sc.symbol_count == 2
    hits = sc.recall(
        "python::load_json def load_json(path): return json.load(open(path))",
        top_k=5,
        threshold=0.3,
    )
    assert hits, "expected at least one recall hit for near-dup query"
    assert all(isinstance(h, SymbolRecall) for h in hits)
    # Sorted descending by similarity.
    sims = [h.similarity for h in hits]
    assert sims == sorted(sims, reverse=True)


# ---------------------------------------------------------------------------
# AC 7 — replace pattern (re-upsert same symbol_id)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAVE_DEPS, reason="requires live embedding stack")
def test_upsert_replace_keeps_symbol_count_stable(tmp_path: Path) -> None:
    sc = EmbeddingSidecar(
        tmp_path, {"index": {"embedding_sidecar": {"enabled": True}}}
    )
    sc.upsert_symbol("sym_a", "def foo(): pass", {"symbol_name": "foo"})
    sc.upsert_symbol("sym_a", "def foo_v2(): return 42", {"symbol_name": "foo"})
    assert sc.symbol_count == 1


# ---------------------------------------------------------------------------
# AC 9 / 10 — save / load with schema enforcement
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAVE_DEPS, reason="save/load needs live HNSW")
def test_save_load_roundtrip(tmp_path: Path) -> None:
    pol = {"index": {"embedding_sidecar": {"enabled": True}}}
    sc = EmbeddingSidecar(tmp_path, pol)
    sc.upsert_symbol("s1", "def a(): pass", {"symbol_name": "a"})
    sc.upsert_symbol("s2", "def b(): pass", {"symbol_name": "b"})
    assert sc.save() is True

    idx_path = tmp_path / ".autofix" / "indexing" / "embedding.hnswlib.idx"
    meta_path = (
        tmp_path / ".autofix" / "indexing" / "embedding-metadata.json"
    )
    assert idx_path.is_file()
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text())
    assert meta["schema_version"] == "embedding_sidecar_v1"
    assert meta["model_name"] == EMBEDDING_SIDECAR_MODEL_NAME
    assert meta["dim"] == EMBEDDING_SIDECAR_DIM

    sc2 = EmbeddingSidecar(tmp_path, pol)
    assert sc2.load() is True
    assert sc2.symbol_count == 2


def test_load_fails_on_missing_files(tmp_path: Path) -> None:
    pol = {"index": {"embedding_sidecar": {"enabled": True}}}
    sc = EmbeddingSidecar(tmp_path, pol)
    assert sc.load() is False


@pytest.mark.skipif(not HAVE_DEPS, reason="needs HNSW to write then corrupt")
def test_load_fails_on_schema_mismatch(tmp_path: Path) -> None:
    pol = {"index": {"embedding_sidecar": {"enabled": True}}}
    sc = EmbeddingSidecar(tmp_path, pol)
    sc.upsert_symbol("s1", "x", {"symbol_name": "x"})
    assert sc.save() is True

    meta_path = (
        tmp_path / ".autofix" / "indexing" / "embedding-metadata.json"
    )
    meta = json.loads(meta_path.read_text())
    meta["schema_version"] = "bogus_v9"
    meta_path.write_text(json.dumps(meta))

    sc2 = EmbeddingSidecar(tmp_path, pol)
    assert sc2.load() is False


# ---------------------------------------------------------------------------
# AC 11 — cold_rebuild emits EmbeddingSidecarColdRebuild with 5 keys
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAVE_DEPS, reason="cold_rebuild needs live embedding")
def test_cold_rebuild_emits_event_with_exact_keys(tmp_path: Path) -> None:
    sc = EmbeddingSidecar(
        tmp_path, {"index": {"embedding_sidecar": {"enabled": True}}}
    )
    stream = [
        {
            "symbol_id": f"s{i}",
            "text": f"def fn{i}(): return {i}",
            "symbol_name": f"fn{i}",
            "language": "python",
            "path": f"x{i}.py",
        }
        for i in range(3)
    ]
    n = sc.cold_rebuild(stream)
    assert n == 3
    assert sc.symbol_count == 3

    events = [
        json.loads(line)
        for line in (tmp_path / ".autofix" / "events.jsonl")
        .read_text()
        .splitlines()
        if line
    ]
    cold = [e for e in events if e.get("event") == "EmbeddingSidecarColdRebuild"]
    assert len(cold) >= 1
    payload = cold[-1]["scan_event"]
    assert set(payload.keys()) == {
        "event_type",
        "repo_id",
        "symbol_count",
        "duration_ms",
        "model_name",
    }
    assert payload["event_type"] == "EmbeddingSidecarColdRebuild"
    assert payload["symbol_count"] == 3
    assert payload["model_name"] == EMBEDDING_SIDECAR_MODEL_NAME


# ---------------------------------------------------------------------------
# AC 12 — symbol_count property
# ---------------------------------------------------------------------------


def test_disabled_symbol_count_is_zero(tmp_path: Path) -> None:
    sc = EmbeddingSidecar(tmp_path, None)
    assert sc.symbol_count == 0


# ---------------------------------------------------------------------------
# AC 20 — EmbeddingSidecarIncrementalUpdate payload shape
# ---------------------------------------------------------------------------


def test_incremental_update_event_payload_has_exact_five_keys(
    tmp_path: Path,
) -> None:
    """AC 20: ``EmbeddingSidecarIncrementalUpdate`` payload pins to the
    exact key set ``event_type, repo_id, symbols_added, symbols_updated,
    duration_ms``. Exercised via the module-level helper so the contract
    is verifiable without the optional ML deps."""
    from autofix.indexing.embedding import (
        emit_incremental_update_event,
    )

    emit_incremental_update_event(
        tmp_path,
        symbols_added=3,
        symbols_updated=1,
        duration_ms=42,
    )
    events = [
        json.loads(line)
        for line in (tmp_path / ".autofix" / "events.jsonl")
        .read_text()
        .splitlines()
        if line
    ]
    updates = [
        e for e in events if e.get("event") == "EmbeddingSidecarIncrementalUpdate"
    ]
    assert len(updates) == 1
    payload = updates[0]["scan_event"]
    assert set(payload.keys()) == {
        "event_type",
        "repo_id",
        "symbols_added",
        "symbols_updated",
        "duration_ms",
    }
    assert payload["event_type"] == "EmbeddingSidecarIncrementalUpdate"
    assert payload["symbols_added"] == 3
    assert payload["symbols_updated"] == 1
    assert payload["duration_ms"] == 42


def test_flush_incremental_update_noops_when_disabled(
    tmp_path: Path,
) -> None:
    sc = EmbeddingSidecar(tmp_path, None)
    assert sc.flush_incremental_update() is False
    assert not (tmp_path / ".autofix" / "events.jsonl").exists()


def test_flush_incremental_update_emits_when_counters_set(
    tmp_path: Path,
) -> None:
    """Bypass the enabled-construction path so the flush emitter can be
    tested without the optional ML deps: force ``_enabled=True`` and seed
    the counters directly. This mirrors how a live enabled sidecar
    accumulates counters via ``upsert_symbol`` — the emit contract is the
    same either way."""
    sc = EmbeddingSidecar(tmp_path, None)
    sc._enabled = True
    sc._added_since_flush = 2
    sc._updated_since_flush = 1
    assert sc.flush_incremental_update(duration_ms=7) is True
    assert sc._added_since_flush == 0
    assert sc._updated_since_flush == 0
    events = [
        json.loads(line)
        for line in (tmp_path / ".autofix" / "events.jsonl")
        .read_text()
        .splitlines()
        if line
    ]
    assert len(events) == 1
    payload = events[0]["scan_event"]
    assert set(payload.keys()) == {
        "event_type",
        "repo_id",
        "symbols_added",
        "symbols_updated",
        "duration_ms",
    }
    assert payload["symbols_added"] == 2
    assert payload["symbols_updated"] == 1
    assert payload["duration_ms"] == 7


def test_flush_incremental_update_no_emit_when_counters_zero(
    tmp_path: Path,
) -> None:
    sc = EmbeddingSidecar(tmp_path, None)
    sc._enabled = True
    # Counters default to 0 from __init__.
    assert sc.flush_incremental_update() is False
    assert not (tmp_path / ".autofix" / "events.jsonl").exists()
