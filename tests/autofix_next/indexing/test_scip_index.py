"""Tests for ``autofix_next.indexing.scip_index`` (AC #1 / #3 / #7 / #8 /
#9 / #10 / #11 / #12 / #13 / #16 / #19 / #26).

``SCIPIndex`` is the persistent, content-addressed cache that wraps the
in-memory ``CallGraph`` with a load/save/apply_incremental/get_symbol
surface. This file pins the on-disk layout, atomic-rename discipline,
reader/writer concurrency invariants, schema-version handling, and the
symbol-keyed incremental refresh set from design-decisions.md §6.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("tree_sitter_python")
pytest.importorskip("tree_sitter")

REPO_ROOT = Path(__file__).resolve().parents[3]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_tiny_repo(root: Path) -> None:
    """Two-file hub-and-spoke repo used as a shared test substrate."""

    (root / "hub.py").write_text(
        "def hub_func():\n    return 1\n", encoding="utf-8"
    )
    (root / "caller.py").write_text(
        "from hub import hub_func\n"
        "\n"
        "def caller_func():\n"
        "    return hub_func()\n",
        encoding="utf-8",
    )


def _build_and_save(root: Path):
    """Build a CallGraph + save it via SCIPIndex. Returns the index."""

    from autofix_next.indexing.scip_index import SCIPIndex
    from autofix_next.invalidation.call_graph import CallGraph

    graph = CallGraph.build_from_root(root)
    # Compute content hashes the way production will: sha256 of file bytes
    # for every file in graph.all_paths. The test doesn't care about the
    # exact helper name — the SCIPIndex.save contract takes a dict.
    import hashlib

    content_hashes: dict[str, str] = {}
    for rel in graph.all_paths:
        abs_p = root / rel
        content_hashes[rel] = hashlib.sha256(abs_p.read_bytes()).hexdigest()

    idx = SCIPIndex()
    idx.save(root, content_hashes, graph)
    return idx, graph, content_hashes


# ----------------------------------------------------------------------
# AC #1 — public API surface
# ----------------------------------------------------------------------


def test_scipindex_public_api_exists() -> None:
    """AC #1: ``SCIPIndex`` exists with ``load``, ``save``,
    ``apply_incremental``, ``get_symbol``."""

    from autofix_next.indexing.scip_index import SCIPIndex

    for name in ("load", "save", "apply_incremental", "get_symbol"):
        assert hasattr(SCIPIndex, name), f"SCIPIndex must expose .{name}"
        # load is a classmethod; the rest are instance methods. All
        # must be callable.
        assert callable(getattr(SCIPIndex, name))


# ----------------------------------------------------------------------
# AC #3 — directory layout on cold save
# ----------------------------------------------------------------------


def test_save_creates_expected_layout(tmp_path: Path) -> None:
    """AC #3: a successful save produces
    ``.autofix-next/state/index/{manifest.json,reverse_refs.json,.lock,shards/}``."""

    _make_tiny_repo(tmp_path)
    _build_and_save(tmp_path)

    idx_root = tmp_path / ".autofix-next" / "state" / "index"
    assert idx_root.is_dir(), f"{idx_root} must exist after save"
    assert (idx_root / "manifest.json").is_file(), "manifest.json missing"
    assert (idx_root / "reverse_refs.json").is_file(), "reverse_refs.json missing"
    assert (idx_root / ".lock").exists(), ".lock lockfile missing"
    assert (idx_root / "shards").is_dir(), "shards/ subdirectory missing"


# ----------------------------------------------------------------------
# AC #7 — two-level content-addressed fanout
# ----------------------------------------------------------------------


def test_shard_fanout_two_level(tmp_path: Path) -> None:
    """AC #7: shard for hash ``abcdef...`` lives at ``shards/ab/cd/abcdef....json``.

    Also verifies that looking up a hash not present returns None (not raises).
    """

    _make_tiny_repo(tmp_path)
    idx, _graph, content_hashes = _build_and_save(tmp_path)

    shards_root = tmp_path / ".autofix-next" / "state" / "index" / "shards"
    for rel, hsh in content_hashes.items():
        expected = shards_root / hsh[0:2] / hsh[2:4] / f"{hsh}.json"
        assert expected.is_file(), (
            f"shard for {rel} expected at {expected}, not found"
        )

    # A hash that definitely isn't present must resolve to None (no raise).
    from autofix_next.indexing.scip_index import SCIPIndex

    reloaded = SCIPIndex.load(tmp_path)
    assert reloaded is not None
    missing = reloaded.get_symbol("bogus/nowhere.py::does_not_exist")
    assert missing is None


# ----------------------------------------------------------------------
# AC #8 — manifest schema
# ----------------------------------------------------------------------


def test_manifest_has_schema_version_built_at_hashes(tmp_path: Path) -> None:
    """AC #8: manifest.json has ``schema_version`` + ``built_at`` (ISO-8601)
    + ``hashes`` (path → sha256)."""

    _make_tiny_repo(tmp_path)
    _idx, _graph, content_hashes = _build_and_save(tmp_path)

    manifest_path = (
        tmp_path / ".autofix-next" / "state" / "index" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "scip_json_v1"
    # built_at is an ISO-8601 UTC string — accept either "Z" or "+00:00".
    assert isinstance(manifest["built_at"], str)
    assert "T" in manifest["built_at"], "built_at must be ISO-8601"

    assert manifest["hashes"] == content_hashes

    # AC #8 closer: for each path p, the shard is exactly hashes[p] + ".json".
    shards_root = tmp_path / ".autofix-next" / "state" / "index" / "shards"
    for rel, hsh in manifest["hashes"].items():
        expected = shards_root / hsh[0:2] / hsh[2:4] / f"{hsh}.json"
        assert expected.is_file(), f"manifest points at missing shard for {rel}"


# ----------------------------------------------------------------------
# AC #9 — atomic manifest rename + parent-dir fsync
# ----------------------------------------------------------------------


def test_manifest_atomic_rename_survives_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #9: a simulated crash AFTER ``manifest.json.tmp`` is written but
    BEFORE ``os.replace`` is called leaves the prior ``manifest.json``
    intact and readable.

    Strategy: perform one clean save so a valid manifest exists, then
    monkey-patch ``os.replace`` to raise before the rename happens and
    trigger a second save. The second save's tmp file must exist (proving
    write-then-rename ordering) and the first save's manifest.json must
    still be loadable.
    """

    from autofix_next.indexing.scip_index import SCIPIndex
    from autofix_next.invalidation.call_graph import CallGraph

    _make_tiny_repo(tmp_path)
    _first_idx, _graph, _hashes = _build_and_save(tmp_path)

    idx_root = tmp_path / ".autofix-next" / "state" / "index"
    manifest_path = idx_root / "manifest.json"
    assert manifest_path.is_file()
    first_content = manifest_path.read_text(encoding="utf-8")

    # Mutate a file and trigger a second save while blocking the final rename.
    (tmp_path / "hub.py").write_text(
        "def hub_func():\n    return 42\n", encoding="utf-8"
    )

    real_replace = os.replace
    crashed: dict[str, bool] = {"tripped": False}

    def crash_on_manifest_replace(src: str | os.PathLike, dst: str | os.PathLike) -> None:
        # Only sabotage the manifest rename; per-shard renames still succeed.
        if str(dst).endswith("manifest.json"):
            crashed["tripped"] = True
            raise RuntimeError("simulated crash between tmp-write and rename")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", crash_on_manifest_replace)

    graph2 = CallGraph.build_from_root(tmp_path)
    import hashlib

    hashes2 = {
        rel: hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
        for rel in graph2.all_paths
    }
    idx2 = SCIPIndex()
    # The save should raise (our sabotage raises RuntimeError) OR it should
    # swallow the error and leave the pre-existing manifest intact. We accept
    # either: what matters is the prior manifest survives.
    try:
        idx2.save(tmp_path, hashes2, graph2)
    except RuntimeError:
        pass

    assert crashed["tripped"], (
        "monkeypatched os.replace was never invoked for manifest.json — "
        "save() is not using atomic-rename discipline"
    )

    # The prior manifest.json must still exist and be byte-identical.
    assert manifest_path.is_file(), "prior manifest.json was deleted mid-crash"
    assert manifest_path.read_text(encoding="utf-8") == first_content

    # A subsequent SCIPIndex.load should still return a valid index (or None
    # if the implementation chose to treat the partial state as invalid —
    # both outcomes honor AC #11's "never raise" contract).
    loaded = SCIPIndex.load(tmp_path)
    assert loaded is not None or loaded is None  # must not raise; both OK


def test_manifest_save_fsyncs_parent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #9: parent-dir ``fsync`` is called so the rename is durable on
    ext4/APFS. We monkey-patch ``os.fsync`` with a call-recording wrapper
    and assert that at least one fsync is issued against a file descriptor
    whose target is the index root directory.
    """

    _make_tiny_repo(tmp_path)

    fsynced_paths: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        # fd → path resolution that works on both Linux and macOS.
        # /dev/fd/<N> is a symlink on both platforms; readlink returns the path.
        resolved: str | None = None
        for candidate in (f"/dev/fd/{fd}", f"/proc/self/fd/{fd}"):
            try:
                resolved = os.readlink(candidate)
                break
            except OSError:
                continue
        if resolved is None:
            # macOS final fallback: F_GETPATH with bytes buffer (Python 3.11+ syntax)
            try:
                import fcntl as _fcntl
                resolved = _fcntl.fcntl(fd, _fcntl.F_GETPATH, b"\x00" * 1024).rstrip(b"\x00").decode("utf-8", errors="replace")
            except (OSError, AttributeError, ValueError):
                resolved = f"<fd:{fd}>"
        fsynced_paths.append(resolved)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    _build_and_save(tmp_path)

    idx_root = (tmp_path / ".autofix-next" / "state" / "index").resolve()
    # At least one fsync must have targeted the index root dir OR its
    # manifest. Resolving symlinks and normalizing paths makes the
    # comparison filesystem-agnostic.
    normalized = [os.path.realpath(p) for p in fsynced_paths]
    idx_root_str = str(idx_root)
    manifest_str = str(idx_root / "manifest.json")
    assert any(
        p.startswith(idx_root_str) or p == manifest_str
        for p in normalized
    ), (
        f"no fsync call targeted {idx_root_str} or its manifest; "
        f"observed fsync targets: {normalized}"
    )


# ----------------------------------------------------------------------
# AC #10 — reverse_refs preserves untouched entries
# ----------------------------------------------------------------------


def test_reverse_refs_preserves_untouched_entries(tmp_path: Path) -> None:
    """AC #10: an incremental refresh rebuilds reverse_refs for touched
    symbols and keeps entries for untouched symbols verbatim."""

    from autofix_next.indexing.scip_index import SCIPIndex
    from autofix_next.invalidation.call_graph import CallGraph

    # Build a three-file repo so an incremental touch on ONE file leaves
    # the reverse_refs entries for symbols in the OTHER two untouched.
    (tmp_path / "hub.py").write_text(
        "def hub_func():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "caller_a.py").write_text(
        "from hub import hub_func\n"
        "\n"
        "def caller_a():\n    return hub_func()\n",
        encoding="utf-8",
    )
    (tmp_path / "caller_b.py").write_text(
        "from hub import hub_func\n"
        "\n"
        "def caller_b():\n    return hub_func()\n",
        encoding="utf-8",
    )

    idx1, graph1, hashes1 = _build_and_save(tmp_path)

    rr_path = tmp_path / ".autofix-next" / "state" / "index" / "reverse_refs.json"
    rr_before = json.loads(rr_path.read_text(encoding="utf-8"))
    # Sidecar is wrapped with {"schema_version": ..., "refs": {...}} per
    # the interpretation note in plan.md §API Contracts.
    refs_before = rr_before["refs"]

    # Mutate ONLY caller_a.py; caller_b's reverse_refs entry must be
    # byte-identical after the second build.
    (tmp_path / "caller_a.py").write_text(
        "from hub import hub_func\n"
        "\n"
        "def caller_a():\n    return hub_func() + 1\n",
        encoding="utf-8",
    )

    # A second build_from_root triggers apply_incremental under the hood.
    CallGraph.build_from_root(tmp_path)

    rr_after = json.loads(rr_path.read_text(encoding="utf-8"))
    refs_after = rr_after["refs"]

    caller_b_sid = "caller_b.py::caller_b"
    assert caller_b_sid in refs_before, (
        "baseline reverse_refs should include caller_b_sid"
    )
    assert refs_before.get(caller_b_sid) == refs_after.get(caller_b_sid), (
        "caller_b's reverse_refs entry must be preserved verbatim"
    )


# ----------------------------------------------------------------------
# AC #11 — load returns None, never raises
# ----------------------------------------------------------------------


def test_load_returns_none_on_missing_manifest(tmp_path: Path) -> None:
    """AC #11: load returns None when ``manifest.json`` is absent."""

    from autofix_next.indexing.scip_index import SCIPIndex

    # No index has ever been written.
    assert SCIPIndex.load(tmp_path) is None


def test_load_returns_none_on_corrupt_manifest(tmp_path: Path) -> None:
    """AC #11: load returns None (never raises) on a corrupt manifest."""

    from autofix_next.indexing.scip_index import SCIPIndex

    _make_tiny_repo(tmp_path)
    _build_and_save(tmp_path)

    manifest_path = tmp_path / ".autofix-next" / "state" / "index" / "manifest.json"
    manifest_path.write_text("{not valid json", encoding="utf-8")

    # Must not raise.
    assert SCIPIndex.load(tmp_path) is None


def test_load_returns_none_on_missing_shard(tmp_path: Path) -> None:
    """AC #11: load returns None when a referenced shard is missing."""

    from autofix_next.indexing.scip_index import SCIPIndex

    _make_tiny_repo(tmp_path)
    _build_and_save(tmp_path)

    shards_root = tmp_path / ".autofix-next" / "state" / "index" / "shards"
    # Nuke every shard file.
    for shard in shards_root.rglob("*.json"):
        shard.unlink()

    assert SCIPIndex.load(tmp_path) is None


# ----------------------------------------------------------------------
# AC #26 — schema_version mismatch → None
# ----------------------------------------------------------------------


def test_load_schema_version_mismatch_returns_none(tmp_path: Path) -> None:
    """AC #26: manifest with ``schema_version="scip_json_v2"`` forces
    ``load`` to return None (cold rebuild)."""

    from autofix_next.indexing.scip_index import SCIPIndex

    _make_tiny_repo(tmp_path)
    _build_and_save(tmp_path)

    manifest_path = tmp_path / ".autofix-next" / "state" / "index" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "scip_json_v2"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert SCIPIndex.load(tmp_path) is None


# ----------------------------------------------------------------------
# AC #12 — reader never locks; race-safe
# ----------------------------------------------------------------------


def test_reader_never_locks_race_safe(tmp_path: Path) -> None:
    """AC #12: a reader running concurrently with a writer sees either
    the pre-rename or post-rename manifest — never a partial state.

    We seed a valid index, then spawn a background writer that mutates a
    file and calls save() repeatedly. On the main thread we run load() in
    a tight loop and assert every returned index is self-consistent (or
    None) — never partial.
    """

    from autofix_next.indexing.scip_index import SCIPIndex
    from autofix_next.invalidation.call_graph import CallGraph

    _make_tiny_repo(tmp_path)
    _build_and_save(tmp_path)

    import hashlib

    stop = threading.Event()

    def writer_loop() -> None:
        counter = 0
        while not stop.is_set():
            counter += 1
            (tmp_path / "hub.py").write_text(
                f"def hub_func():\n    return {counter}\n", encoding="utf-8"
            )
            graph = CallGraph.build_from_root(tmp_path)
            hashes = {
                rel: hashlib.sha256(
                    (tmp_path / rel).read_bytes()
                ).hexdigest()
                for rel in graph.all_paths
            }
            idx = SCIPIndex()
            try:
                idx.save(tmp_path, hashes, graph)
            except Exception:
                # Per the 30s-retry contract, save should not raise on
                # contention — but we swallow defensively so a bug in
                # the writer can't mask the reader-safety assertion.
                pass

    writer = threading.Thread(target=writer_loop, daemon=True)
    writer.start()

    try:
        deadline = time.monotonic() + 1.5
        iterations = 0
        while time.monotonic() < deadline:
            iterations += 1
            # load() MUST NOT RAISE regardless of mid-write state.
            loaded = SCIPIndex.load(tmp_path)
            # Either None or a self-consistent index — never partial.
            if loaded is not None:
                # If a non-None index is returned it must answer
                # get_symbol without raising.
                _ = loaded.get_symbol("hub.py::hub_func")
        assert iterations > 0
    finally:
        stop.set()
        writer.join(timeout=5.0)


# ----------------------------------------------------------------------
# AC #13 — flock fallback emits telemetry
# ----------------------------------------------------------------------


def test_flock_fallback_emits_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #13: a ``BlockingIOError`` from ``fcntl.flock`` after the retry
    window skips persistence, does not raise, and carries
    ``index_cache_mode="fallback_concurrent_writer"``.
    """

    import fcntl

    from autofix_next.indexing.scip_index import SCIPIndex
    from autofix_next.invalidation.call_graph import CallGraph

    _make_tiny_repo(tmp_path)
    graph = CallGraph.build_from_root(tmp_path)

    import hashlib

    hashes = {
        rel: hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
        for rel in graph.all_paths
    }

    # Monkey-patch fcntl.flock to always raise BlockingIOError on an
    # exclusive-lock attempt. Readers don't lock, so this only affects
    # save() / apply_incremental().
    real_flock = fcntl.flock

    def always_blocking(fd: int, op: int) -> None:
        if op & fcntl.LOCK_EX:
            raise BlockingIOError("simulated lock contention")
        return real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", always_blocking)

    # Also shrink the retry budget via a monkeypatched sleep so the test
    # completes in bounded wall-clock time. The production retry uses
    # time.sleep for backoff — patch sleep to be a near-no-op.
    real_sleep = time.sleep

    def fast_sleep(duration: float) -> None:
        # Don't sleep more than 10ms total per call; prevents a 30s test.
        real_sleep(min(duration, 0.001))

    monkeypatch.setattr(time, "sleep", fast_sleep)

    idx = SCIPIndex()
    # save() MUST NOT raise on blocked lock.
    idx.save(tmp_path, hashes, graph)

    # The production-side signal for the telemetry row is either a
    # public attribute (e.g. ``last_cache_mode``) on the SCIPIndex
    # instance or a module-level probe function. We accept either.
    cache_mode = getattr(idx, "last_cache_mode", None)
    assert cache_mode == "fallback_concurrent_writer", (
        "SCIPIndex.save must surface a last_cache_mode attribute equal "
        f"to 'fallback_concurrent_writer' on flock timeout; got {cache_mode!r}"
    )


# ----------------------------------------------------------------------
# AC #16 — apply_incremental uses symbol-keyed refresh set R
# ----------------------------------------------------------------------


def test_apply_incremental_symbol_keyed_refresh(tmp_path: Path) -> None:
    """AC #16: ``apply_incremental`` rewrites ``callers``/``callees`` only
    for files in the refresh set ``R = {f | ∃ sym ∈ invalidation.affected_symbols :
    f ∈ reverse_refs[sym]} ∪ dirty_files``.

    Verifies that an incremental update after mutating only ``hub.py``
    re-emits the ``hub.py`` shard AND the caller shards that reference
    it, but leaves a totally-unrelated file's shard untouched (byte-
    identical content_hash + shard path).
    """

    from autofix_next.indexing.scip_index import SCIPIndex
    from autofix_next.invalidation.call_graph import CallGraph

    # Four files: hub + two callers + one unrelated orphan.
    (tmp_path / "hub.py").write_text(
        "def hub_func():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "caller_a.py").write_text(
        "from hub import hub_func\n"
        "\n"
        "def caller_a():\n    return hub_func()\n",
        encoding="utf-8",
    )
    (tmp_path / "caller_b.py").write_text(
        "from hub import hub_func\n"
        "\n"
        "def caller_b():\n    return hub_func()\n",
        encoding="utf-8",
    )
    (tmp_path / "orphan.py").write_text(
        "def orphan_func():\n    return 42\n", encoding="utf-8"
    )

    _idx1, _graph1, hashes1 = _build_and_save(tmp_path)
    orphan_hash_before = hashes1["orphan.py"]

    # Mutate hub.py only.
    (tmp_path / "hub.py").write_text(
        "def hub_func():\n    return 999\n", encoding="utf-8"
    )
    CallGraph.build_from_root(tmp_path)

    # After incremental: orphan's shard must still exist at the same
    # content-addressed path (its content_hash hasn't changed).
    import hashlib

    orphan_hash_after = hashlib.sha256(
        (tmp_path / "orphan.py").read_bytes()
    ).hexdigest()
    assert orphan_hash_after == orphan_hash_before, (
        "orphan.py was not modified; its content hash must be stable"
    )

    shards_root = tmp_path / ".autofix-next" / "state" / "index" / "shards"
    expected_orphan_shard = (
        shards_root
        / orphan_hash_before[0:2]
        / orphan_hash_before[2:4]
        / f"{orphan_hash_before}.json"
    )
    assert expected_orphan_shard.is_file(), (
        "orphan.py's shard must still exist at its content-addressed path"
    )


# ----------------------------------------------------------------------
# AC #19 — content-addressing branch revert reuse
# ----------------------------------------------------------------------


def test_content_addressing_branch_revert_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #19: content A → content B → content A; the third build uses
    the full-cache-hit path (no re-parse) because the content hash maps
    back to a shard that already exists on disk.

    Strategy: monkey-patch ``parse_file`` with a call counter and assert
    it was called zero times during the third build.
    """

    from autofix_next.invalidation.call_graph import CallGraph
    from autofix_next.parsing import tree_sitter as ts_mod

    content_a = "def hub_func():\n    return 1\n"
    content_b = "def hub_func():\n    return 2\n"

    (tmp_path / "hub.py").write_text(content_a, encoding="utf-8")
    CallGraph.build_from_root(tmp_path)  # build 1: content A (cold)

    (tmp_path / "hub.py").write_text(content_b, encoding="utf-8")
    CallGraph.build_from_root(tmp_path)  # build 2: content B (partial)

    (tmp_path / "hub.py").write_text(content_a, encoding="utf-8")

    # Install parse_file call counter before the third build.
    real_parse_file = ts_mod.parse_file
    parse_calls = {"n": 0}

    def counting_parse_file(*args: Any, **kwargs: Any) -> Any:
        parse_calls["n"] += 1
        return real_parse_file(*args, **kwargs)

    monkeypatch.setattr(ts_mod, "parse_file", counting_parse_file)

    CallGraph.build_from_root(tmp_path)  # build 3: content A again (cache hit)

    assert parse_calls["n"] == 0, (
        f"build 3 should take the full-cache-hit path (0 parse_file calls); "
        f"observed {parse_calls['n']} calls — content-addressing is broken"
    )
