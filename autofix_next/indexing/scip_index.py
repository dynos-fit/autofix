"""Persistent, content-addressed SCIP index (AC #1 / #3 / #7 / #8 / #9 / #10 /
#11 / #12 / #13 / #16 / #26).

:class:`SCIPIndex` wraps the in-memory
:class:`autofix_next.invalidation.call_graph.CallGraph` with a cache
persisted under ``<root>/.autofix-next/state/index/``. The cache
consists of:

* ``manifest.json`` — authoritative record of the current index
  schema version, build timestamp, and per-file content hashes (AC #8).
* ``reverse_refs.json`` — sidecar mapping ``symbol_id -> [path, ...]``
  listing every file that references each symbol (AC #10).
* ``shards/<h[0:2]>/<h[2:4]>/<h>.json`` — one per-file ``scip_json_v1``
  shard at a two-level content-addressed fanout (AC #7).
* ``.lock`` — a zero-byte file used with :func:`fcntl.flock` for
  mutual-exclusion between writers only — readers never lock
  (AC #12 / #13).

Atomic-rename discipline
------------------------
Every write that touches ``manifest.json`` or ``reverse_refs.json`` uses
a strict 4-step sequence:

1. Write ``<path>.tmp`` and ``flush`` it.
2. ``fsync`` the tmp file's descriptor.
3. ``fsync`` the parent directory's descriptor (crash-durability on
   ext4 / APFS — without this a crash-after-rename can drop the new
   directory entry on some filesystems).
4. ``os.replace(tmp, final)`` and then ``fsync`` the parent directory
   once more so the post-rename directory entry is durable.

The sidecar is renamed BEFORE the manifest so a reader that observes
the new manifest also observes the matching sidecar (AC #10 /
pipeline.md §API Contracts).

Concurrency model
-----------------
Writers (``save`` / ``apply_incremental``) acquire ``.lock`` with
:func:`fcntl.flock` ``LOCK_EX | LOCK_NB`` and retry with exponential
backoff up to 30 seconds. Readers (``load``) never lock — they rely on
the atomic-rename discipline above so the worst they can see is the
pre-rename state. A :class:`BlockingIOError` after the 30 s window
skips persistence, sets ``self.last_cache_mode =
"fallback_concurrent_writer"``, and does NOT raise (AC #13). The actual
telemetry row emission is seg-2's responsibility; seg-1 just exposes
the signal via the attribute.

What lives outside this module
------------------------------
* The CallGraph wrapper that calls ``load`` / ``save`` /
  ``apply_incremental`` from ``build_from_root`` is seg-2's work. Seg-1
  only ships the index itself.
* The ``InvalidationComputed`` telemetry row is emitted by
  :mod:`autofix_next.funnel.pipeline`; we only set the flag.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from autofix_next.indexing.scip_emitter import (
    SCIP_JSON_SCHEMA_VERSION,
    _validate_shard_shape,
    emit_document,
)

# ----------------------------------------------------------------------
# On-disk layout constants (AC #3 / #7 / #8)
# ----------------------------------------------------------------------

# Directory the whole index lives under. Deliberately rooted at
# ``.autofix-next/`` so we never write into the legacy ``.autofix/``
# locked path (AC #24).
INDEX_ROOT_REL: str = ".autofix-next/state/index"

MANIFEST_FILENAME: str = "manifest.json"
REVERSE_REFS_FILENAME: str = "reverse_refs.json"
LOCK_FILENAME: str = ".lock"
SHARDS_DIRNAME: str = "shards"

# Retry-with-backoff parameters for flock. Mirrors design-decisions.md
# §7 and plan.md API Contracts. Total budget 30 s; initial delay 50 ms
# doubling each attempt up to a 1 s cap.
LOCK_TIMEOUT_SECONDS: float = 30.0
LOCK_INITIAL_BACKOFF: float = 0.05
LOCK_MAX_BACKOFF: float = 1.0

# Signal string written to ``self.last_cache_mode`` when flock acquisition
# times out. Seg-2's pipeline.py reads this to populate the
# ``index_cache_mode`` field on the ``InvalidationComputed`` telemetry row.
CACHE_MODE_FALLBACK: str = "fallback_concurrent_writer"


# ----------------------------------------------------------------------
# SCIPIndex
# ----------------------------------------------------------------------


class SCIPIndex:
    """Persistent, content-addressed SCIP index.

    Construct with ``SCIPIndex()`` for a fresh in-memory state that
    hasn't yet loaded any cache. Populated state is produced by
    :meth:`load` (reading from disk) or :meth:`save` (serializing a
    :class:`CallGraph`).

    Public surface (AC #1):

    * :meth:`load` — classmethod; never raises; returns ``None`` on
      any invalidity.
    * :meth:`save` — full cold-build persistence under flock.
    * :meth:`apply_incremental` — symbol-keyed refresh.
    * :meth:`get_symbol` — O(1) lookup after load, never raises.
    """

    def __init__(self) -> None:
        # Set by ``load`` or a successful ``save``. Remains ``None`` on a
        # fresh instance that has not yet touched the disk.
        self._manifest: dict | None = None
        # Wrapped sidecar: ``{"schema_version": ..., "refs": {...}}``.
        self._reverse_refs: dict | None = None
        # In-memory shard cache, keyed by content_hash. Lazily populated
        # by ``get_symbol`` / ``apply_incremental`` so cold-start load
        # cost is bounded by the manifest size, not by total shard count.
        self._shard_cache: dict[str, dict] = {}
        # Exposed signal for seg-2's telemetry layer. ``None`` until an
        # operation yields a non-default cache mode.
        self.last_cache_mode: str | None = None
        # The root the index belongs to. Stamped by ``load`` and on each
        # successful ``save`` so ``get_symbol`` can locate shards without
        # the caller threading ``root`` through every call site.
        self._root: Path | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, root: Path) -> "SCIPIndex | None":
        """Load the on-disk index at ``root`` or return ``None`` (AC #11 / #26).

        Never raises. Every invalidity — missing manifest, corrupt JSON,
        schema-version mismatch, missing shard, malformed sidecar —
        collapses to a ``None`` return so the caller can fall back to a
        cold rebuild.

        Readers never acquire the ``.lock`` file (AC #12). The worst
        observable state in the presence of a concurrent writer is the
        pre-rename manifest + pre-rename sidecar, because both writes go
        through :meth:`_atomic_write_json`.
        """

        index_dir = root / INDEX_ROOT_REL
        manifest_path = index_dir / MANIFEST_FILENAME

        # ---- Manifest ------------------------------------------------
        if not manifest_path.is_file():
            return None
        try:
            manifest_text = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(manifest, dict):
            return None
        if manifest.get("schema_version") != SCIP_JSON_SCHEMA_VERSION:
            # AC #26: wrong schema → None (forces cold rebuild).
            return None
        hashes = manifest.get("hashes")
        if not isinstance(hashes, dict):
            return None
        # Each hash value must be a string; malformed entries mean a
        # rebuild is cheaper than guessing around the damage.
        for rel, hsh in hashes.items():
            if not isinstance(rel, str) or not isinstance(hsh, str):
                return None
        if "built_at" not in manifest or not isinstance(
            manifest.get("built_at"), str
        ):
            return None

        # ---- Reverse-refs sidecar -----------------------------------
        refs_path = index_dir / REVERSE_REFS_FILENAME
        if not refs_path.is_file():
            return None
        try:
            refs_text = refs_path.read_text(encoding="utf-8")
            reverse_refs = json.loads(refs_text)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(reverse_refs, dict):
            return None
        if reverse_refs.get("schema_version") != SCIP_JSON_SCHEMA_VERSION:
            return None
        if not isinstance(reverse_refs.get("refs"), dict):
            return None

        # ---- Shard existence check ----------------------------------
        # We do NOT load every shard here — just verify the referenced
        # file exists on disk. Lazy loading in ``get_symbol`` keeps the
        # fast-path cheap while still catching a manifest that points at
        # a shard file that's been deleted out from under us.
        for rel, hsh in hashes.items():
            shard_path = cls._shard_path_for_hash(root, hsh)
            if not shard_path.is_file():
                return None

        idx = cls()
        idx._manifest = manifest
        idx._reverse_refs = reverse_refs
        idx._root = root
        return idx

    def save(
        self,
        root: Path,
        content_hashes: dict[str, str],
        graph: Any,
    ) -> None:
        """Persist the full graph under ``root`` (AC #3 / #7 / #8 / #9 / #13).

        Writes one shard per file in ``content_hashes``, rebuilds the
        reverse-refs sidecar, and finally renames the manifest into
        place using the 4-step atomic-rename discipline.

        On :class:`BlockingIOError` after the 30 s flock window, sets
        ``self.last_cache_mode = CACHE_MODE_FALLBACK`` and returns
        without persisting — the in-memory graph is still valid, the
        cache just didn't get updated this run. Never raises.
        """

        index_dir = root / INDEX_ROOT_REL
        try:
            index_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # We can't even create the index directory (permission,
            # read-only FS, etc.). Fall back silently — the caller
            # already has a valid in-memory graph.
            self.last_cache_mode = CACHE_MODE_FALLBACK
            return

        lock_path = index_dir / LOCK_FILENAME
        try:
            lock_path.touch(exist_ok=True)
        except OSError:
            self.last_cache_mode = CACHE_MODE_FALLBACK
            return

        try:
            with self._acquire_lock(lock_path):
                self._write_all_shards(root, content_hashes, graph)
                reverse_refs_map = self._compute_reverse_refs_from_graph(
                    graph
                )
                self._atomic_write_json(
                    index_dir / REVERSE_REFS_FILENAME,
                    {
                        "schema_version": SCIP_JSON_SCHEMA_VERSION,
                        "refs": reverse_refs_map,
                    },
                )
                manifest = {
                    "schema_version": SCIP_JSON_SCHEMA_VERSION,
                    "built_at": _utc_iso8601_now(),
                    "hashes": dict(content_hashes),
                }
                self._atomic_write_json(
                    index_dir / MANIFEST_FILENAME, manifest
                )
                # Update in-memory state on successful persist.
                self._manifest = manifest
                self._reverse_refs = {
                    "schema_version": SCIP_JSON_SCHEMA_VERSION,
                    "refs": reverse_refs_map,
                }
                self._root = root
                self.last_cache_mode = None
        except BlockingIOError:
            # 30 s flock timeout — documented fallback path (AC #13).
            self.last_cache_mode = CACHE_MODE_FALLBACK
            return

    def apply_incremental(
        self,
        invalidation: Any,
        root: Path,
        graph_builder: Callable[[Path], Any],
    ) -> None:
        """Incrementally refresh the index against ``invalidation`` (AC #16).

        Computes the refresh set ``R = {f | ∃ sym ∈
        invalidation.affected_symbols : f ∈ reverse_refs[sym]} ∪
        dirty_files``. Re-emits shards for ``dirty_files`` (where
        ``dirty_files = {f ∈ invalidation.affected_files | new_hash(f)
        != manifest.hashes[f]}``). Rewrites ``callers`` / ``callees``
        inline for every file in ``R``. Rebuilds the reverse-refs entries
        for ``invalidation.affected_symbols`` while preserving untouched
        entries verbatim (AC #10).

        On flock timeout, sets ``self.last_cache_mode`` and returns; no
        raise (AC #13).
        """

        index_dir = root / INDEX_ROOT_REL
        try:
            index_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.last_cache_mode = CACHE_MODE_FALLBACK
            return

        lock_path = index_dir / LOCK_FILENAME
        try:
            lock_path.touch(exist_ok=True)
        except OSError:
            self.last_cache_mode = CACHE_MODE_FALLBACK
            return

        try:
            with self._acquire_lock(lock_path):
                self._apply_incremental_locked(
                    invalidation, root, graph_builder
                )
                self.last_cache_mode = None
        except BlockingIOError:
            self.last_cache_mode = CACHE_MODE_FALLBACK
            return

    def get_symbol(self, symbol_id: str) -> dict | None:
        """Return the inline ``SymbolEntry`` dict for ``symbol_id`` or ``None``.

        Lazy-loads the host shard on demand. Returns ``None`` for an
        unknown id, a malformed shard, or any I/O failure reading the
        shard file. Never raises.

        The ``symbol_id`` format is ``"<path>::<qualified-name>"`` (AC #8
        in task-003). We recover the host path by splitting on the first
        ``"::"`` and look up its content hash in the manifest to find
        the shard on disk.
        """

        if not isinstance(symbol_id, str):
            return None
        if "::" not in symbol_id:
            return None
        if self._manifest is None or self._root is None:
            return None

        path, _, _ = symbol_id.partition("::")
        hashes = self._manifest.get("hashes")
        if not isinstance(hashes, dict):
            return None
        content_hash = hashes.get(path)
        if not isinstance(content_hash, str):
            return None

        shard = self._load_shard(self._root, content_hash)
        if shard is None:
            return None
        for sym in shard.get("symbols", []):
            if sym.get("symbol_id") == symbol_id:
                return sym
        return None

    # ------------------------------------------------------------------
    # Private helpers — locking + atomic writes
    # ------------------------------------------------------------------

    @contextmanager
    def _acquire_lock(self, lock_path: Path) -> Iterator[int]:
        """Acquire ``lock_path`` as a file lock with retry-backoff (AC #13).

        Uses :func:`fcntl.flock` with ``LOCK_EX | LOCK_NB`` so every
        attempt is non-blocking. A failed attempt sleeps for the current
        backoff interval (starting at 50 ms, doubling each attempt,
        capped at 1 s) and retries until :data:`LOCK_TIMEOUT_SECONDS`
        elapses. After the budget, raises :class:`BlockingIOError` so the
        caller's ``except BlockingIOError`` branch can trip the
        fallback-mode signal.
        """

        # Open the lockfile — we explicitly open rather than rely on a
        # cached descriptor so each acquire / release cycle is
        # self-contained and there's no risk of leaking descriptors
        # across threads / processes.
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            backoff = LOCK_INITIAL_BACKOFF
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    # Standard contention path — retry until deadline.
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(min(backoff, max(0.0, deadline - time.monotonic())))
                    backoff = min(backoff * 2, LOCK_MAX_BACKOFF)
                except OSError as exc:
                    # POSIX sometimes surfaces EWOULDBLOCK/EAGAIN as
                    # ``OSError`` rather than ``BlockingIOError``. Treat
                    # them as equivalent for lock-contention purposes.
                    if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                        if time.monotonic() >= deadline:
                            raise BlockingIOError(
                                f"flock timeout after {LOCK_TIMEOUT_SECONDS}s"
                            ) from exc
                        time.sleep(
                            min(
                                backoff,
                                max(0.0, deadline - time.monotonic()),
                            )
                        )
                        backoff = min(backoff * 2, LOCK_MAX_BACKOFF)
                        continue
                    raise
            try:
                yield fd
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    # Best-effort unlock; the descriptor close below
                    # implicitly releases any held lock anyway.
                    pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _atomic_write_json(self, final_path: Path, obj: dict) -> None:
        """Atomically replace ``final_path`` with ``obj`` (AC #9).

        Sequence:

        1. Write ``<final>.tmp`` and ``flush``.
        2. ``fsync`` the tmp file descriptor.
        3. ``fsync`` the parent directory so the tmp entry is durable.
        4. ``os.replace(tmp, final)``.
        5. ``fsync`` the parent directory again so the renamed entry is
           durable.

        A crash between step 1 and step 4 leaves the prior ``final_path``
        intact (AC #9 crash-rename contract). A crash between step 4 and
        step 5 still leaves the new entry — ``os.replace`` is atomic
        with respect to readers even before the parent-dir fsync.
        """

        tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
        data = json.dumps(obj, sort_keys=True, indent=2).encode("utf-8")

        # Step 1 + 2: write + fsync the tmp file. Open with O_TRUNC so a
        # leftover tmp from a prior crashed write is cleanly replaced.
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o644,
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError(
                        f"unexpected zero-length write to {tmp_path}"
                    )
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

        parent_dir = final_path.parent

        # Step 3: fsync parent directory so the new tmp entry is durable.
        self._fsync_directory(parent_dir)

        # Step 4: atomic rename.
        os.replace(str(tmp_path), str(final_path))

        # Step 5: fsync parent directory again so the renamed entry is
        # durable. This is the belt-and-braces guarantee AC #9 asks for.
        self._fsync_directory(parent_dir)

    @staticmethod
    def _fsync_directory(dir_path: Path) -> None:
        """``fsync`` the directory's descriptor — no-op on unsupported OSes.

        Some filesystems (notably parts of NFS) reject ``fsync`` on a
        directory fd. Swallow the resulting ``OSError`` because the
        crash-durability guarantee is an opportunistic best-effort; if
        the FS doesn't support it we're not going to raise to a caller
        that can't do anything about it.
        """

        try:
            fd = os.open(str(dir_path), os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Private helpers — shard IO
    # ------------------------------------------------------------------

    @staticmethod
    def _shard_path_for_hash(root: Path, content_hash: str) -> Path:
        """Compute the two-level fanout shard path for a content hash (AC #7).

        Hash ``abcdef...`` lands at
        ``<root>/.autofix-next/state/index/shards/ab/cd/abcdef....json``.
        The caller is responsible for creating the parent directories;
        :meth:`_write_shard` does that before writing.
        """

        return (
            root
            / INDEX_ROOT_REL
            / SHARDS_DIRNAME
            / content_hash[:2]
            / content_hash[2:4]
            / f"{content_hash}.json"
        )

    def _write_shard(
        self, root: Path, path: str, content_hash: str, graph: Any
    ) -> None:
        """Build and atomically persist the shard for ``path`` (AC #7)."""

        doc = emit_document(
            path=path,
            graph=graph,
            content_hash=content_hash,
        )
        shard_path = self._shard_path_for_hash(root, content_hash)
        try:
            shard_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Cannot create the fanout directory — fall through; the
            # subsequent write will raise and be caught upstream.
            pass
        self._atomic_write_json(shard_path, doc)
        # Cache the freshly-written shard so subsequent ``get_symbol``
        # calls don't re-read it from disk.
        self._shard_cache[content_hash] = doc

    def _write_all_shards(
        self,
        root: Path,
        content_hashes: dict[str, str],
        graph: Any,
    ) -> None:
        """Write a shard for every file in ``content_hashes``."""

        for path, content_hash in content_hashes.items():
            try:
                self._write_shard(root, path, content_hash, graph)
            except OSError:
                # Per-shard I/O failure: skip the offending file but
                # keep going. The manifest written later will not list
                # the skipped shard, so ``load`` won't try to read it.
                continue

    def _load_shard(self, root: Path, content_hash: str) -> dict | None:
        """Load + memo a shard by content hash. Returns ``None`` on any error."""

        cached = self._shard_cache.get(content_hash)
        if cached is not None:
            return cached
        shard_path = self._shard_path_for_hash(root, content_hash)
        if not shard_path.is_file():
            return None
        try:
            text = shard_path.read_text(encoding="utf-8")
            shard = json.loads(text)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(shard, dict):
            return None
        try:
            _validate_shard_shape(shard)
        except ValueError:
            return None
        self._shard_cache[content_hash] = shard
        return shard

    # ------------------------------------------------------------------
    # Private helpers — reverse_refs
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_reverse_refs_from_graph(graph: Any) -> dict[str, list[str]]:
        """Compute a fresh ``{symbol_id: [path, ...]}`` map from ``graph``.

        AC #10 wording: "``reverse_refs.json`` is a sidecar mapping
        ``symbol_id -> list[repo-relative-path]`` of every file that
        references that symbol."

        A symbol is *referenced* by:

        * The file it's declared in (the definition itself counts as a
          reference — without this convention a leaf function with no
          callers would be absent from the sidecar, and an incremental
          refresh that needs to preserve its untouched entry has nothing
          to preserve).
        * Every file that hosts a caller (one entry per distinct file).

        Every symbol in the graph gets an entry — even leaf functions
        with no callers — so the "preserve untouched entries" contract
        has something concrete to preserve (AC #10 preservation test).
        """

        refs: dict[str, set[str]] = {}
        callers_src: dict[str, Any] = getattr(graph, "_callers", {}) or {}
        symbols_src: dict[str, Any] = getattr(graph, "_symbols", {}) or {}

        # Seed with every declared symbol — self-reference via definition.
        for sid, info in symbols_src.items():
            path = getattr(info, "path", None)
            if isinstance(path, str):
                refs.setdefault(sid, set()).add(path)
            elif "::" in sid:
                refs.setdefault(sid, set()).add(sid.split("::", 1)[0])

        # Layer on every caller's host file.
        for callee_sid, callers in callers_src.items():
            for caller_sid in callers:
                caller_info = symbols_src.get(caller_sid)
                if caller_info is None:
                    if "::" in caller_sid:
                        caller_path = caller_sid.split("::", 1)[0]
                    else:
                        continue
                else:
                    caller_path = caller_info.path
                refs.setdefault(callee_sid, set()).add(caller_path)

        # Return a deterministic, sorted-list-of-paths-per-symbol map so
        # the sidecar is byte-identical between equivalent rebuilds.
        return {
            sid: sorted(paths) for sid, paths in sorted(refs.items())
        }

    # ------------------------------------------------------------------
    # Private helpers — apply_incremental
    # ------------------------------------------------------------------

    def _apply_incremental_locked(
        self,
        invalidation: Any,
        root: Path,
        graph_builder: Callable[[Path], Any],
    ) -> None:
        """Incremental refresh body; runs with ``.lock`` held.

        Implementation follows AC #16 step-by-step:

        1. Compute ``dirty_files`` from ``invalidation.affected_files``
           (paths whose on-disk sha256 no longer matches the manifest).
        2. Build the refresh set ``R`` by unioning ``dirty_files`` with
           every file that references a symbol in
           ``invalidation.affected_symbols`` (via the sidecar).
        3. Ask ``graph_builder(root)`` for the freshly-built
           :class:`CallGraph`. We delegate rather than build here so seg-2's
           ``build_from_root`` wrapper can own the one-call-per-build
           contract.
        4. Re-emit shards for every file in ``dirty_files``.
        5. Rewrite shards (callers / callees inline) for every file in
           ``R \\ dirty_files`` since the graph rebuild may have changed
           edge endpoints.
        6. Rebuild reverse-refs entries for the affected symbols;
           preserve untouched entries verbatim.
        7. Atomic-rename the sidecar, then the manifest.
        """

        index_dir = root / INDEX_ROOT_REL

        # The manifest may be absent if a prior cold save was aborted —
        # defensive fallback to an empty shape so we still rebuild.
        prev_manifest = self._manifest or {"hashes": {}}
        prev_hashes: dict[str, str] = dict(
            prev_manifest.get("hashes") or {}
        )
        prev_reverse_refs_wrap = self._reverse_refs or {
            "schema_version": SCIP_JSON_SCHEMA_VERSION,
            "refs": {},
        }
        prev_refs: dict[str, list[str]] = dict(
            prev_reverse_refs_wrap.get("refs") or {}
        )

        affected_files = tuple(
            getattr(invalidation, "affected_files", ()) or ()
        )
        affected_symbols = frozenset(
            getattr(invalidation, "affected_symbols", frozenset())
            or frozenset()
        )

        # ---- dirty_files --------------------------------------------
        # A file is dirty when (a) it's on-disk and its sha256 differs
        # from the prior manifest or (b) it's new (not in the manifest).
        # Deleted files are NOT treated as dirty shards (there's nothing
        # to rewrite) but their entries in the manifest / sidecar are
        # dropped below.
        dirty_files: set[str] = set()
        deleted_files: set[str] = set()
        fresh_hashes: dict[str, str] = {}

        for rel in affected_files:
            abs_path = root / rel
            if not rel.endswith(".py"):
                # Non-py paths pass through without shard updates.
                continue
            try:
                is_file = abs_path.is_file()
            except OSError:
                is_file = False
            if not is_file:
                deleted_files.add(rel)
                continue
            try:
                content_bytes = abs_path.read_bytes()
            except OSError:
                # Transient read failure — treat as absent.
                deleted_files.add(rel)
                continue
            new_hash = hashlib.sha256(content_bytes).hexdigest()
            fresh_hashes[rel] = new_hash
            if prev_hashes.get(rel) != new_hash:
                dirty_files.add(rel)

        # ---- refresh set R ------------------------------------------
        # Files that host a symbol referenced by something in
        # ``affected_symbols`` per the sidecar. Entries outside
        # affected_symbols are preserved verbatim (AC #10).
        r_set: set[str] = set(dirty_files)
        for sid in affected_symbols:
            for path in prev_refs.get(sid, ()):
                r_set.add(path)

        # ---- Build the fresh graph ----------------------------------
        # graph_builder is expected to return a populated CallGraph for
        # ``root`` including edges for files in R. The builder owns the
        # parse cost; we only re-emit shards.
        graph = graph_builder(root)

        # ---- Re-emit shards -----------------------------------------
        # The union of files we need to re-emit: dirty_files (content
        # changed) and r_set (edges may have changed). Files that are
        # in the prior manifest but untouched by the invalidation keep
        # their existing shard.
        to_emit: set[str] = set(r_set) | set(dirty_files)

        # Compute new hashes for files in ``r_set`` that aren't in
        # ``dirty_files`` — those shards need to be re-written under a
        # content-addressed path that matches their on-disk bytes.
        for rel in list(to_emit):
            if rel in fresh_hashes:
                continue
            abs_path = root / rel
            try:
                if not abs_path.is_file():
                    deleted_files.add(rel)
                    to_emit.discard(rel)
                    continue
                content_bytes = abs_path.read_bytes()
            except OSError:
                deleted_files.add(rel)
                to_emit.discard(rel)
                continue
            fresh_hashes[rel] = hashlib.sha256(content_bytes).hexdigest()

        # Start the new manifest as a copy of the prior one, then layer
        # fresh hashes on top and drop deleted files.
        new_hashes: dict[str, str] = dict(prev_hashes)
        for rel, hsh in fresh_hashes.items():
            new_hashes[rel] = hsh
        for rel in deleted_files:
            new_hashes.pop(rel, None)

        for rel in to_emit:
            content_hash = new_hashes.get(rel)
            if content_hash is None:
                continue
            try:
                self._write_shard(root, rel, content_hash, graph)
            except OSError:
                continue

        # ---- Rebuild reverse-refs for affected symbols --------------
        # Preserve entries for symbols NOT in affected_symbols verbatim
        # (AC #10); rebuild entries for affected_symbols from the fresh
        # graph.
        full_refs = self._compute_reverse_refs_from_graph(graph)
        new_refs: dict[str, list[str]] = dict(prev_refs)
        for sid in affected_symbols:
            fresh = full_refs.get(sid)
            if fresh is None:
                new_refs.pop(sid, None)
            else:
                new_refs[sid] = fresh
        # Drop entries for symbols that reference deleted files exclusively.
        for sid in list(new_refs.keys()):
            # Strip deleted paths from each entry; drop the entry if
            # empty.
            cleaned = [p for p in new_refs[sid] if p not in deleted_files]
            if cleaned:
                new_refs[sid] = cleaned
            else:
                new_refs.pop(sid, None)

        # ---- Persist sidecar + manifest -----------------------------
        self._atomic_write_json(
            index_dir / REVERSE_REFS_FILENAME,
            {
                "schema_version": SCIP_JSON_SCHEMA_VERSION,
                "refs": new_refs,
            },
        )
        manifest = {
            "schema_version": SCIP_JSON_SCHEMA_VERSION,
            "built_at": _utc_iso8601_now(),
            "hashes": new_hashes,
        }
        self._atomic_write_json(index_dir / MANIFEST_FILENAME, manifest)

        # Update in-memory state.
        self._manifest = manifest
        self._reverse_refs = {
            "schema_version": SCIP_JSON_SCHEMA_VERSION,
            "refs": new_refs,
        }
        self._root = root


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _utc_iso8601_now() -> str:
    """Return the current UTC time formatted as an ISO-8601 string (AC #8).

    We use ``YYYY-MM-DDTHH:MM:SSZ`` with ``Z`` as the timezone suffix
    (equivalent to ``+00:00``). The test accepts either.
    """

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


__all__ = [
    "CACHE_MODE_FALLBACK",
    "INDEX_ROOT_REL",
    "LOCK_FILENAME",
    "LOCK_TIMEOUT_SECONDS",
    "MANIFEST_FILENAME",
    "REVERSE_REFS_FILENAME",
    "SHARDS_DIRNAME",
    "SCIPIndex",
]
