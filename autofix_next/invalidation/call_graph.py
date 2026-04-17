"""In-memory call graph for cross-file invalidation (AC #4 / #5 / #6 / #7 / #8 / #10),
with the seg-2 SCIP-index fast path (AC #14 / #15 / #17 / #18 / #19).

Responsibilities
----------------
* Model a single symbol (function / class / class method) as an immutable
  :class:`SymbolInfo` record. The ``symbol_id`` format is
  ``"<repo-relative-path>::<qualified-name>"`` where the qualified name is
  ``name`` for top-level functions and classes, and ``"ClassName.method"``
  for methods declared inside a class body (AC #8).
* Maintain four private dicts on :class:`CallGraph` (AC #4):

    - ``_symbols``          — ``symbol_id → SymbolInfo``.
    - ``_callees``          — ``caller_id → set[callee_id]`` (downstream).
    - ``_callers``          — ``callee_id → set[caller_id]`` (upstream;
                              populated in parallel with ``_callees`` so
                              the dual-direction invariant of AC #10 holds).
    - ``_path_to_symbols``  — ``repo-relative path → set[symbol_id]`` so
                              :meth:`symbols_in` is an O(1) dict lookup.

* Expose a small, frozen-set-based public surface (AC #5): ``symbols_in``,
  ``callers_of`` (BFS upward over ``_callers``), ``__getitem__`` for
  ``SymbolInfo`` lookup, and the ``all_symbols`` / ``all_paths`` /
  ``symbol_count`` properties.

* Provide :meth:`CallGraph.build_from_root` (AC #7 / #8 / #10 / #14-#19), a
  classmethod that:

    1. Enumerates ``*.py`` files under ``root`` and computes their sha256
       content hashes (AC #19 content-addressing seed).
    2. Consults :meth:`SCIPIndex.load` first (AC #15 / #17).

       * If the persisted manifest's hashes match the freshly-computed
         hashes byte-for-byte, populate the graph from shards and return
         without parsing any file (full-cache-hit path, AC #15).
       * If the persisted manifest exists but some hashes have drifted,
         build an :class:`Invalidation` from the dirty set and run
         :meth:`SCIPIndex.apply_incremental` to refresh only the affected
         shards.
       * Otherwise, fall through to a cold build and persist via
         :meth:`SCIPIndex.save` (AC #17).

    3. Cold build runs the task-003 two-pass walker:

       * **Pass 1** — parses every enumerated file with
         :func:`autofix_next.parsing.tree_sitter.parse_file` and collects
         top-level ``function_definition`` / ``class_definition`` plus
         ``function_definition`` nodes nested **one** level inside a class
         body. Functions nested inside other functions are deliberately
         excluded (AC #8). Syntax-error files are silently skipped so a
         single broken source can't kill the whole build.
       * **Pass 2** — walks each file's symbol table
         (:func:`autofix_next.indexing.symbols.build_symbol_table`), then
         calls :func:`_resolve_edges_v2` which caches a single
         ``known_paths`` frozenset per build so per-reference resolution
         reduces to a dict membership check (AC #18).

Imports of :mod:`autofix_next.invalidation.import_resolver` and
:mod:`autofix_next.indexing.scip_index` are deferred to method bodies so
this module stays importable when the resolver / SCIP index haven't been
wired yet, and so downstream callers can construct and poke at an empty
:class:`CallGraph` without triggering the cache machinery.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

# Directories excluded from the ``os.walk`` fallback (AC #7). The set is
# intentionally narrow: only build artefacts, VCS metadata, vendored
# virtualenvs, and the autofix output directories. Kept module-private
# because callers don't need to override it — the git-ls-files branch
# honours ``.gitignore`` on its own.
_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        "build",
        "dist",
        ".autofix",
        ".autofix-next",
        ".dynos",
    }
)


@dataclass(slots=True, frozen=True)
class SymbolInfo:
    """Immutable metadata for a single collected symbol (AC #4 / #8).

    Attributes
    ----------
    symbol_id:
        Stable cross-file identifier, ``"<relpath>::<qualified-name>"``.
        For a top-level ``def foo`` in ``pkg/mod.py`` this is
        ``"pkg/mod.py::foo"``; for ``def method`` inside ``class Klass``
        in the same file it is ``"pkg/mod.py::Klass.method"``.
    path:
        Repo-relative POSIX path of the file the symbol is declared in.
    name:
        Qualified name (``"foo"`` or ``"Klass.method"``). Matches the
        right-hand side of ``symbol_id``.
    kind:
        ``"function"`` (plain function or class method) or ``"class"``.
    start_line / end_line:
        1-indexed line range spanning the full node in the source file.
        ``end_line`` is inclusive.
    """

    symbol_id: str
    path: str
    name: str
    kind: Literal["function", "class"]
    start_line: int
    end_line: int


class CallGraph:
    """Directed call graph of Python symbols across a repo (AC #4).

    The graph is a plain in-memory structure. Callers construct it via
    :meth:`build_from_root` in production; tests may also instantiate it
    with :class:`CallGraph.__new__` and hand-populate the private dicts.
    """

    __slots__ = (
        "_symbols",
        "_callees",
        "_callers",
        "_path_to_symbols",
        "_root",
        "_repo_root",
        # ``__dict__`` is included alongside the named slots so the
        # invalidation planner's fresh-instance test (AC #14) can
        # monkey-patch :meth:`callers_of` with a sentinel that raises on
        # accidental traversal. The named slots still provide fast, typo-
        # safe access for the documented fields; ``__dict__`` only matters
        # when a caller deliberately overrides a bound method.
        "__dict__",
    )

    def __init__(self) -> None:
        # The four dicts documented in the module docstring (AC #4). Kept
        # as plain builtins so hand-built test fixtures can poke at them
        # without routing through a builder method.
        self._symbols: dict[str, SymbolInfo] = {}
        self._callees: dict[str, set[str]] = {}
        self._callers: dict[str, set[str]] = {}
        self._path_to_symbols: dict[str, set[str]] = {}
        self._root: Path | None = None
        # ``_repo_root`` is the canonical handle the invalidation planner
        # uses to locate on-disk files for new-file on-the-fly parsing
        # (AC #16) and existence checks (AC #17 / #18). ``build_from_root``
        # stamps this alongside ``_root``; tests that hand-build a graph via
        # ``CallGraph.__new__`` may also assign it directly (slot reserved
        # above).
        self._repo_root: Path | None = None

    # ------------------------------------------------------------------
    # Public read surface (AC #5)
    # ------------------------------------------------------------------

    def symbols_in(self, path: str) -> frozenset[str]:
        """Return the symbols declared in ``path`` (repo-relative).

        Returns an empty frozenset when ``path`` is not known to the graph
        — callers use this to distinguish "no symbols here" from "this
        file was never scanned" at their own discretion.
        """

        return frozenset(self._path_to_symbols.get(path, ()))

    def callers_of(
        self, symbol_ids: Iterable[str], max_depth: int
    ) -> frozenset[str]:
        """Return symbols that transitively reach ``symbol_ids`` (AC #6).

        A breadth-first walk upward over ``_callers``. Semantics pinned by
        the unit tests:

        * ``max_depth <= 0`` → empty frozenset (no expansion).
        * The seeds themselves are **never** included in the return value.
        * Cycles are visited at most once.
        * Passing a bare ``str`` raises :class:`TypeError` — a bare string
          would otherwise iterate character-by-character and silently
          produce nonsense seeds.
        """

        if isinstance(symbol_ids, str):
            # Iterable[str] includes str itself, which would silently
            # shatter into per-character "symbol ids". Reject explicitly.
            raise TypeError(
                "symbol_ids must be an iterable of str, not str itself"
            )
        if max_depth <= 0:
            return frozenset()

        seeds = set(symbol_ids)
        # ``visited`` tracks everything we've already enqueued (seeds +
        # any caller we've pushed into the frontier) so a cycle can't
        # cause us to revisit the same node.
        visited: set[str] = set(seeds)
        frontier: set[str] = set(seeds)
        result: set[str] = set()

        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for node in frontier:
                for caller in self._callers.get(node, ()):
                    if caller in visited:
                        continue
                    visited.add(caller)
                    result.add(caller)
                    next_frontier.add(caller)
            if not next_frontier:
                # Fully exhausted the reachable caller set; no point
                # spinning through the remaining depth budget.
                break
            frontier = next_frontier
        return frozenset(result)

    def __getitem__(self, symbol_id: str) -> SymbolInfo:
        """Return the :class:`SymbolInfo` for ``symbol_id``.

        Raises :class:`KeyError` on an unknown id — matches dict semantics
        so callers can use ``try/except KeyError`` or ``in`` before
        indexing.
        """

        return self._symbols[symbol_id]

    @property
    def all_symbols(self) -> frozenset[str]:
        """All symbol ids currently in the graph, as a frozenset."""

        return frozenset(self._symbols)

    @property
    def all_paths(self) -> frozenset[str]:
        """All file paths that contributed at least one symbol."""

        return frozenset(self._path_to_symbols)

    @property
    def symbol_count(self) -> int:
        """Total number of symbols — used by telemetry / invalidation events."""

        return len(self._symbols)

    # ------------------------------------------------------------------
    # Builder (AC #7 / #8 / #10 / #14 / #15 / #17 / #19)
    # ------------------------------------------------------------------

    @classmethod
    def build_from_root(cls, root: Path) -> "CallGraph":
        """Build a fully-populated :class:`CallGraph` by scanning ``root``.

        The method runs in three possible modes, in order:

        1. **Full cache hit (AC #15)** — if a prior :class:`SCIPIndex`
           exists at ``root`` and its manifest's ``hashes`` exactly match
           the freshly-computed ``{path: sha256}`` map for the current
           on-disk sources, populate a :class:`CallGraph` from the
           persisted shards without calling ``parse_file`` on any file.
        2. **Partial hit / incremental refresh (AC #17)** — if a prior
           index exists but some hashes have drifted, construct an
           :class:`Invalidation` over the dirty files and hand off to
           :meth:`SCIPIndex.apply_incremental`, which refreshes the
           affected shards in place and rebuilds the in-memory graph
           off the refreshed index.
        3. **Cold build (AC #17)** — no prior index, corrupt index, or
           schema-mismatched index. Runs task-003's two-pass walker and
           then persists via :meth:`SCIPIndex.save` so the next call can
           take the full-cache-hit path.

        ``graph.last_cache_mode`` carries the index-layer telemetry signal
        (currently only ``"fallback_concurrent_writer"`` from a flock
        timeout, or ``None`` for a clean run). Seg-2's pipeline.py reads
        this attribute to thread ``index_cache_mode`` into the
        ``InvalidationComputed`` row.
        """

        # Lazy imports — neither SCIPIndex nor the planner's Invalidation
        # shape is required for the cold-only code path, and keeping them
        # out of module import time means this module stays importable
        # when the index layer is under test or monkey-patched.
        from autofix_next.indexing.scip_index import SCIPIndex

        # --- Step 1: enumerate + hash ---------------------------------
        rel_paths = _enumerate_python_files(root)
        content_hashes: dict[str, str] = {}
        for rel in rel_paths:
            abs_path = root / rel
            try:
                content_hashes[rel] = _sha256_of_file(abs_path)
            except OSError:
                # File vanished or became unreadable between enumeration
                # and hashing; skip it — the cold build handles the same
                # race by simply having zero symbols for that file.
                continue

        # --- Step 2: try loading the persisted index -----------------
        idx = SCIPIndex.load(root)

        # --- Step 3a: full cache hit (AC #15 / #19) -------------------
        # AC #15: manifest hashes exactly match current hashes → populate
        # from shards, zero parse_file calls.
        # AC #19 (branch A→B→A revert): even when the manifest's hashes
        # don't match, every current hash may already have a
        # content-addressed shard on disk from a prior build. When that's
        # true we can ALSO skip parse_file entirely — rebuild a
        # CallGraph directly from the existing shards keyed by the
        # current hashes, and refresh the manifest (cheap, no parsing)
        # so the next run's fast path hits the simpler manifest-equal
        # branch.
        if idx is not None and _hashes_equal(
            idx._manifest.get("hashes") if idx._manifest else None,
            content_hashes,
        ):
            graph = cls()
            graph._root = root
            graph._repo_root = root
            cls._populate_from_index(graph, idx)
            graph.last_cache_mode = idx.last_cache_mode  # type: ignore[attr-defined]
            return graph

        # --- Step 3a': content-addressed shard reuse (AC #19) --------
        if idx is not None and cls._all_shards_exist(idx, content_hashes):
            graph = cls._rebuild_from_shards_by_hashes(
                idx, root, content_hashes
            )
            if graph is not None:
                # Persist the refreshed manifest (pointing at the existing
                # content-addressed shards) so the next run hits the
                # simpler manifest-equal full-hit branch. ``save`` rewrites
                # shards too — cheap because emit_document just reads the
                # in-memory graph we just reconstructed from those shards.
                # On flock timeout we still return the rebuilt graph; the
                # cache just doesn't advance this run.
                from autofix_next.indexing.scip_index import SCIPIndex as _SI

                fresh_idx = _SI()
                fresh_idx.save(root, content_hashes, graph)
                graph.last_cache_mode = fresh_idx.last_cache_mode  # type: ignore[attr-defined]
                return graph

        # --- Step 3b: partial hit → incremental refresh (AC #17) -----
        if idx is not None:
            try:
                refreshed = cls._apply_incremental_refresh(
                    idx, root, content_hashes
                )
            except Exception:
                # AC #9 crash-survive: on any exception mid-incremental
                # (e.g., simulated os.replace crash), the prior manifest
                # on disk remains authoritative (atomic-rename discipline).
                # Fall back: populate from the pre-refresh idx snapshot so
                # the caller gets a usable graph instead of a raised error.
                refreshed = cls()
                refreshed._repo_root = root  # type: ignore[attr-defined]
                cls._populate_from_index(refreshed, idx)
            # After ``apply_incremental`` returns, the index's manifest
            # reflects the refreshed state. If the flock path timed out
            # the index is unchanged but we still have a valid cold graph
            # to return; ``last_cache_mode`` surfaces the signal.
            graph = refreshed
            graph.last_cache_mode = idx.last_cache_mode  # type: ignore[attr-defined]
            return graph

        # --- Step 3c: cold build (AC #17) ----------------------------
        graph = cls._cold_build(root)

        # Persist via a freshly-constructed SCIPIndex. We ask
        # ``SCIPIndex.save`` to do the atomic-rename dance; it also sets
        # ``last_cache_mode`` on the index instance on flock timeout.
        new_idx = SCIPIndex()
        new_idx.save(root, content_hashes, graph)
        graph.last_cache_mode = new_idx.last_cache_mode  # type: ignore[attr-defined]
        return graph

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    @classmethod
    def _cold_build(cls, root: Path) -> "CallGraph":
        """Run task-003's two-pass walker and return a populated graph.

        Extracted from the body of :meth:`build_from_root` so the
        incremental-refresh path can hand it off to
        :meth:`SCIPIndex.apply_incremental` via a ``graph_builder``
        callable, and so the cold-fallthrough path can call it directly.
        Does **not** touch :class:`SCIPIndex` — persistence is the
        caller's responsibility.
        """

        graph = cls()
        graph._root = root
        graph._repo_root = root

        rel_paths = _enumerate_python_files(root)

        # Lazy import keeps this module importable when tree-sitter isn't
        # installed (the grammar is only required at build time).
        from autofix_next.parsing.tree_sitter import (
            TreeSitterLoadError,
            parse_file,
        )

        parse_results: dict[str, Any] = {}
        for relpath in rel_paths:
            abs_path = root / relpath
            try:
                pr = parse_file(abs_path, repo_root=root)
            except TreeSitterLoadError:
                # Grammar load/parse failure for this specific file;
                # skip it rather than aborting the whole build.
                continue
            except (FileNotFoundError, OSError):
                # File disappeared between enumeration and parse, or is
                # unreadable (permission, symlink cycle, etc.). Skip.
                continue
            # Tree-sitter is error-tolerant — a malformed source still
            # parses to a tree, but ``root_node.has_error`` is True. The
            # shape of such a tree is unreliable (partial / synthesized
            # nodes) so we skip the whole file: no symbols, no edges.
            # This satisfies the "syntax-error files contribute zero
            # symbols" contract implied by AC #10.
            if getattr(pr.tree.root_node, "has_error", False):
                continue

            parse_results[relpath] = pr

            for sym in _extract_symbols(pr, relpath):
                graph._symbols[sym.symbol_id] = sym
                graph._path_to_symbols.setdefault(relpath, set()).add(
                    sym.symbol_id
                )

        # Pass 2 — edge resolution via the cached known_paths frozenset
        # (AC #18). Factored into a module-level function so tests can
        # target it directly and the class body stays readable.
        _populate_edges_from_parse_results(graph, parse_results)

        return graph

    @classmethod
    def _apply_incremental_refresh(
        cls,
        idx: Any,
        root: Path,
        content_hashes: dict[str, str],
    ) -> "CallGraph":
        """Refresh ``idx`` against the dirty set and return a fresh graph.

        The flow:

        1. Compute dirty files (hash mismatch) + new files (missing from
           manifest) + deleted files (present in manifest but not on
           disk).
        2. Build an :class:`Invalidation` over the union of dirty+new+
           deleted paths and the affected symbols hosted by dirty+deleted
           files (so the index's R-set union covers "files that referenced
           the old definition").
        3. Call :meth:`SCIPIndex.apply_incremental` with a ``graph_builder``
           that closes over :meth:`_cold_build`.
        4. Return the freshly-rebuilt graph. ``apply_incremental`` uses
           the same builder, so we ask it to build once and reuse the
           result as our return value.

        If ``apply_incremental`` falls back (flock timeout), the index is
        unchanged but the cold-built graph is still valid — the caller
        simply doesn't benefit from the partial-refresh persistence on
        this run. ``idx.last_cache_mode`` carries the signal through.
        """

        from autofix_next.invalidation.planner import Invalidation
        from autofix_next.events.schema import ChangeSet

        prev_hashes: dict[str, str] = {}
        if idx._manifest is not None:
            prev_hashes = dict(idx._manifest.get("hashes") or {})

        dirty: set[str] = set()
        for rel, new_hash in content_hashes.items():
            if prev_hashes.get(rel) != new_hash:
                dirty.add(rel)
        # Paths present in the manifest but absent on disk now — treat as
        # deleted so the refresh drops them from the next manifest.
        deleted: set[str] = {
            rel for rel in prev_hashes.keys() if rel not in content_hashes
        }
        affected_files = tuple(sorted(dirty | deleted))

        # Symbols to refresh: every symbol declared in a dirty-or-deleted
        # file, as known from the previous reverse-refs sidecar. We use
        # the sidecar keys whose path-list contains any dirty-or-deleted
        # file — this is what the index's R-set expansion expects.
        affected_symbols: set[str] = set()
        prev_refs_wrap = getattr(idx, "_reverse_refs", None) or {}
        prev_refs: dict[str, list[str]] = dict(prev_refs_wrap.get("refs") or {})
        changed_paths = dirty | deleted
        for sid, paths in prev_refs.items():
            if any(p in changed_paths for p in paths):
                affected_symbols.add(sid)

        # We need an Invalidation with four attributes the index reads:
        # ``affected_files`` and ``affected_symbols``. The real
        # :class:`Invalidation` dataclass requires ``source_changeset``
        # etc., which we don't have here — synthesize a minimal
        # ChangeSet.
        synthetic_changeset = ChangeSet(
            paths=tuple(sorted(changed_paths)),
            watcher_confidence="partial_hit",
            is_fresh_instance=False,
        )
        invalidation = Invalidation(
            affected_symbols=frozenset(affected_symbols),
            affected_files=affected_files,
            is_full_sweep=False,
            depth_used=0,
            source_changeset=synthetic_changeset,
        )

        # ``apply_incremental`` expects a ``graph_builder(root) -> graph``
        # callable. We stash the built graph so we can return it to our
        # caller — the index only needs it for shard emission.
        built: dict[str, "CallGraph"] = {}

        def _builder(r: Path) -> "CallGraph":
            g = cls._cold_build(r)
            built["graph"] = g
            return g

        idx.apply_incremental(invalidation, root, graph_builder=_builder)

        # ``apply_incremental`` may short-circuit (flock timeout) without
        # ever calling the builder. If so, synthesize a cold build now so
        # we always return a populated graph.
        graph = built.get("graph")
        if graph is None:
            graph = cls._cold_build(root)
        return graph

    @classmethod
    def _all_shards_exist(
        cls, idx: Any, content_hashes: dict[str, str]
    ) -> bool:
        """Return True iff every current hash has a shard on disk (AC #19).

        Content-addressed reuse: a shard for hash ``h`` lives at
        ``<root>/.autofix-next/state/index/shards/<h[0:2]>/<h[2:4]>/<h>.json``
        regardless of which build first wrote it. A branch revert that
        brings back a previously-seen file content maps to the same hash,
        hence the same shard path — if the file exists, we can skip the
        parse entirely.
        """

        root = getattr(idx, "_root", None)
        if root is None:
            return False
        # An empty repo has no shards to check; the caller should have
        # already tripped the manifest-equal full-hit branch for that
        # case. Play safe and require at least one hash.
        if not content_hashes:
            return False
        for content_hash in content_hashes.values():
            if not isinstance(content_hash, str):
                return False
            shard_path = idx._shard_path_for_hash(root, content_hash)
            try:
                if not shard_path.is_file():
                    return False
            except OSError:
                return False
        return True

    @classmethod
    def _rebuild_from_shards_by_hashes(
        cls,
        idx: Any,
        root: Path,
        content_hashes: dict[str, str],
    ) -> "CallGraph | None":
        """Rebuild a CallGraph from shards keyed by ``content_hashes`` (AC #19).

        Mirrors :meth:`_populate_from_index` but sources the shard list
        from the CURRENT hashes rather than the (stale) manifest. Returns
        ``None`` if any shard fails to load — the caller falls back to
        the incremental-refresh path.
        """

        graph = cls()
        graph._root = root
        graph._repo_root = root

        for rel, content_hash in content_hashes.items():
            shard = idx._load_shard(root, content_hash)
            if shard is None:
                # A shard listed by hash is missing or malformed —
                # abandon the fast path; the caller reverts to refresh.
                return None
            for sym in shard.get("symbols", []):
                try:
                    sid = sym["symbol_id"]
                    path = sym["path"]
                    name = sym["name"]
                    kind_raw = sym["kind"]
                    start_line = int(sym["start_line"])
                    end_line = int(sym["end_line"])
                    callers_list = list(sym.get("callers") or [])
                    callees_list = list(sym.get("callees") or [])
                except (KeyError, TypeError, ValueError):
                    continue
                kind: Literal["function", "class"] = (
                    "class" if kind_raw == "class" else "function"
                )
                graph._symbols[sid] = SymbolInfo(
                    symbol_id=sid,
                    path=path,
                    name=name,
                    kind=kind,
                    start_line=start_line,
                    end_line=end_line,
                )
                graph._path_to_symbols.setdefault(path, set()).add(sid)
                for caller_sid in callers_list:
                    if not isinstance(caller_sid, str):
                        continue
                    graph._callers.setdefault(sid, set()).add(caller_sid)
                    graph._callees.setdefault(caller_sid, set()).add(sid)
                for callee_sid in callees_list:
                    if not isinstance(callee_sid, str):
                        continue
                    graph._callees.setdefault(sid, set()).add(callee_sid)
                    graph._callers.setdefault(callee_sid, set()).add(sid)

        return graph

    @classmethod
    def _populate_from_index(cls, graph: "CallGraph", idx: Any) -> None:
        """Fill ``graph`` from the persisted shards in ``idx`` (AC #15).

        Reads each shard referenced by the manifest, materializes every
        declared symbol into ``_symbols`` / ``_path_to_symbols``, and
        rebuilds the ``_callers`` / ``_callees`` maps from the inline
        ``callers`` / ``callees`` lists in the shards. No parse_file
        calls happen here — this is the shard-only reconstruction path.
        """

        manifest = idx._manifest
        if not isinstance(manifest, dict):
            return
        hashes = manifest.get("hashes")
        if not isinstance(hashes, dict):
            return

        for rel, content_hash in hashes.items():
            if not isinstance(rel, str) or not isinstance(content_hash, str):
                continue
            shard = idx._load_shard(idx._root, content_hash)
            if shard is None:
                continue
            for sym in shard.get("symbols", []):
                try:
                    sid = sym["symbol_id"]
                    path = sym["path"]
                    name = sym["name"]
                    kind_raw = sym["kind"]
                    start_line = int(sym["start_line"])
                    end_line = int(sym["end_line"])
                    callers_list = list(sym.get("callers") or [])
                    callees_list = list(sym.get("callees") or [])
                except (KeyError, TypeError, ValueError):
                    # A malformed entry shouldn't kill the whole
                    # reconstruction; skip this one and keep going.
                    continue

                # Coerce schema's "method" back to SymbolInfo's
                # "function" since the task-003 dataclass only admits
                # "function" / "class".
                kind: Literal["function", "class"] = (
                    "class" if kind_raw == "class" else "function"
                )

                graph._symbols[sid] = SymbolInfo(
                    symbol_id=sid,
                    path=path,
                    name=name,
                    kind=kind,
                    start_line=start_line,
                    end_line=end_line,
                )
                graph._path_to_symbols.setdefault(path, set()).add(sid)

                # Inline callers / callees — stored on the callee side
                # in the shard (AC #5). Populate both directions so the
                # AC #10 dual-direction invariant holds.
                for caller_sid in callers_list:
                    if not isinstance(caller_sid, str):
                        continue
                    graph._callers.setdefault(sid, set()).add(caller_sid)
                    graph._callees.setdefault(caller_sid, set()).add(sid)
                for callee_sid in callees_list:
                    if not isinstance(callee_sid, str):
                        continue
                    graph._callees.setdefault(sid, set()).add(callee_sid)
                    graph._callers.setdefault(callee_sid, set()).add(sid)


# ----------------------------------------------------------------------
# File enumeration + symbol extraction
# ----------------------------------------------------------------------


def _enumerate_python_files(root: Path) -> list[str]:
    """Return the set of repo-relative ``*.py`` paths under ``root`` (AC #7).

    Uses ``git ls-files '*.py'`` when the directory is a git working tree
    (the fast, .gitignore-aware path). Falls back to :func:`os.walk` with
    :data:`_EXCLUDE_DIRS` pruned when git is unavailable, the directory is
    not a repo, or the subprocess times out.
    """

    try:
        proc = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # ``git`` not on PATH, ran too long, or some other IO failure —
        # fall through to the os.walk branch.
        proc = None

    if proc is not None and proc.returncode == 0:
        # ``git ls-files *.py`` returns paths relative to ``cwd``. Filter
        # and normalize so the result is stable across platforms.
        results = [
            line.strip()
            for line in proc.stdout.splitlines()
            if line.strip().endswith(".py")
        ]
        return sorted(results)

    # os.walk fallback — prunes the excluded directories in-place so we
    # never descend into ``.venv``, ``node_modules``, ``.git``, etc.
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            abs_path = Path(dirpath) / fn
            try:
                rel = abs_path.relative_to(root)
            except ValueError:
                # Shouldn't happen — os.walk only yields under ``root`` —
                # but skip defensively if a symlink jumps outside.
                continue
            out.append(rel.as_posix())
    return sorted(out)


def _sha256_of_file(abs_path: Path) -> str:
    """Return the sha256 hex digest of ``abs_path`` contents (AC #19).

    Reads in a streaming 64 KiB loop so even multi-megabyte sources don't
    balloon memory. Raises :class:`OSError` on read failure — the caller
    is expected to skip the file on error.
    """

    hasher = hashlib.sha256()
    with abs_path.open("rb") as fh:
        while True:
            chunk = fh.read(64 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _hashes_equal(
    lhs: dict[str, str] | None, rhs: dict[str, str] | None
) -> bool:
    """Return True iff two ``{path: sha256}`` maps are byte-identical."""

    if lhs is None or rhs is None:
        return False
    if len(lhs) != len(rhs):
        return False
    for k, v in lhs.items():
        if rhs.get(k) != v:
            return False
    return True


def _extract_symbols(parse_result: Any, relpath: str) -> list[SymbolInfo]:
    """Collect symbols from a single parsed file (AC #8).

    Contract:

    * Every top-level ``function_definition`` and ``class_definition`` is
      a symbol.
    * Inside each class body, every *direct child* ``function_definition``
      is a symbol with the name ``"ClassName.method"``.
    * Functions nested inside other functions are **not** symbols.
    * Nodes with missing names (syntax errors mid-definition) are skipped.
    """

    tree = parse_result.tree
    source_bytes = parse_result.source_bytes
    symbols: list[SymbolInfo] = []

    def _name_of(node: Any) -> str | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        return source_bytes[name_node.start_byte : name_node.end_byte].decode(
            "utf-8", errors="replace"
        )

    def _mk(
        node: Any,
        local_name: str,
        qualified_name: str,
        kind: Literal["function", "class"],
    ) -> SymbolInfo:
        # tree-sitter row positions are 0-indexed; convert to the
        # conventional 1-indexed line numbers used everywhere else in
        # the codebase (see ImportRecord.start_line for parity).
        #
        # ``qualified_name`` goes into the ``symbol_id`` so the id is
        # globally unique inside the file (AC #8: ``Klass.method``);
        # ``local_name`` is stored in ``.name`` so callers can pretty-print
        # or match against bare identifiers that tree-sitter emits during
        # the reference walk.
        return SymbolInfo(
            symbol_id=f"{relpath}::{qualified_name}",
            path=relpath,
            name=local_name,
            kind=kind,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )

    def _unwrap(child: Any) -> Any:
        """Unwrap ``decorated_definition`` to its inner function/class.

        Tree-sitter represents ``@dataclass class X: ...`` (and any other
        decorated top-level definition) as a ``decorated_definition`` node
        whose inner ``definition`` child is the real class/function node.
        Collapsing the wrapper lets the top-level walk treat decorated
        classes identically to bare ones — required for AC #8 + AC #23.
        """
        if child.type == "decorated_definition":
            inner = child.child_by_field_name("definition")
            if inner is not None:
                return inner
            for sub in child.children:
                if sub.type in ("function_definition", "class_definition"):
                    return sub
        return child

    root_node = tree.root_node
    for raw_child in root_node.children:
        child = _unwrap(raw_child)
        if child.type == "function_definition":
            nm = _name_of(child)
            if nm is not None:
                symbols.append(_mk(child, nm, nm, "function"))
            # Do NOT descend into the function body — AC #8 forbids
            # collecting functions nested inside functions.
        elif child.type == "class_definition":
            cls_name = _name_of(child)
            if cls_name is None:
                continue
            symbols.append(_mk(child, cls_name, cls_name, "class"))

            # Enter the class body exactly one level to pick up methods.
            # Methods may themselves be decorated (@staticmethod, @property
            # etc.) so unwrap each class-body child the same way.
            body = child.child_by_field_name("body")
            if body is None:
                continue
            for raw_member in body.children:
                member = _unwrap(raw_member)
                if member.type != "function_definition":
                    continue
                method_name = _name_of(member)
                if method_name is None:
                    continue
                # Symbol id is ``Klass.method`` (AC #8); ``.name`` is the
                # bare ``method`` so the reference-walk map can match it
                # against the identifier tree-sitter emits at call sites.
                symbols.append(
                    _mk(
                        member,
                        method_name,
                        f"{cls_name}.{method_name}",
                        "function",
                    )
                )

    return symbols


# ----------------------------------------------------------------------
# Edge resolution (AC #18): precomputed known_paths + per-reference resolve
# ----------------------------------------------------------------------


def _resolve_edges_v2(
    graph: "CallGraph",
    parse_results: dict[str, Any],
    root: Path,
) -> None:
    """Populate ``graph._callees`` / ``graph._callers`` via the per-file walker.

    Replaces task-003's legacy edge-resolve body which was
    ``O(files × refs × |all_paths|)``. This revision:

    * Caches the ``known_paths`` frozenset once per build so the resolver's
      containment check is a dict membership test (not a walk).
    * Delegates absolute- and relative-import resolution to the single
      ``import_resolver.resolve`` entry point, which internally uses
      ``pathlib`` joinpath + ``relative_to`` for O(len(parts)) resolution.
    * Batches the reference-walk per file using each file's ``SymbolTable``
      so identifier references are joined against ``import_map`` in one pass.

    Contract preserved from task-003's legacy edge-resolution:

    * Edges populate BOTH ``_callees`` and ``_callers`` so the AC #10
      dual-direction invariant holds.
    * Self-edges (``caller == callee``) are elided.
    * Per-file resolver failures downgrade to "no edge", never raise.
    * Same-file references still resolve against the file's own symbol
      table so intra-file calls show up as edges.
    """

    # Lazy imports — same pattern as task-003. Keeps the module importable
    # even in bootstrap environments where the resolver doesn't yet exist.
    try:
        from autofix_next.invalidation.import_resolver import resolve
    except ImportError:
        # Resolver not yet available; leave the graph edge-free.
        # Planner fallback handles the "no edges" case gracefully.
        return
    from autofix_next.indexing.symbols import build_symbol_table

    if root is None:
        return

    # ``known_paths`` (frozen) is the set of .py files we managed to
    # parse cleanly this build. Passed to the resolver which uses it for
    # the "is this target under the repo root" containment check.
    known_paths = frozenset(parse_results.keys()) | frozenset(
        graph._path_to_symbols.keys()
    )

    for relpath, pr in parse_results.items():
        try:
            symtab = build_symbol_table(pr)
        except Exception:
            # A malformed tree-sitter tree can surface AttributeError on
            # missing fields; treat it the same as "no symbols" rather
            # than crashing the whole build.
            continue

        # --- Map bound import names in this file → target symbol id --
        import_map: dict[str, str] = {}
        for record in symtab.imports:
            target_sid = _resolve_import_record(
                record,
                known_paths=known_paths,
                repo_root=root,
                resolve_fn=resolve,
                graph_symbols=graph._symbols,
                source_file=relpath,
            )
            if target_sid is not None:
                import_map[record.bound_name] = target_sid

        # --- Same-file (local) symbol map ----------------------------
        local_sym_ids = graph._path_to_symbols.get(relpath, set())
        local_map: dict[str, str] = {}
        for sid in local_sym_ids:
            info = graph._symbols[sid]
            # Top-level functions / classes are keyed by their bare name;
            # methods are keyed by ``"Klass.method"`` which tree-sitter
            # never emits as a single identifier, so they naturally only
            # appear as call edges when the method name itself is
            # referenced somewhere else.
            local_map.setdefault(info.name, sid)

        # --- Resolved targets referenced from this file --------------
        referenced_targets: set[str] = set()
        for ref_name in symtab.references:
            if ref_name in import_map:
                referenced_targets.add(import_map[ref_name])
            elif ref_name in local_map:
                referenced_targets.add(local_map[ref_name])

        # --- Wire edges (both directions, AC #10) --------------------
        for caller_sid in local_sym_ids:
            for callee_sid in referenced_targets:
                if caller_sid == callee_sid:
                    # Self-edges add noise without changing reachability
                    # for the planner; skip them.
                    continue
                graph._callees.setdefault(caller_sid, set()).add(callee_sid)
                graph._callers.setdefault(callee_sid, set()).add(caller_sid)


def _resolve_import_record(
    record: Any,
    *,
    known_paths: frozenset[str],
    repo_root: Path,
    resolve_fn: Any,
    graph_symbols: dict[str, SymbolInfo],
    source_file: str | None = None,
) -> str | None:
    """Resolve one :class:`ImportRecord` to a ``symbol_id`` or ``None``.

    Delegates to ``import_resolver.resolve`` which handles absolute, aliased,
    and relative imports. For ``from x import y`` cases the resolver returns
    the target ``(path, symbol)``; this helper composes the final ``symbol_id``
    and checks that the target symbol exists in the graph's collected set
    (absolute ``from x import y`` where ``y`` is a symbol inside
    ``x``, relative imports, etc.).
    """

    # The resolver path already handles module-vs-symbol disambiguation
    # for ``from`` imports. Letting it own that logic keeps our fast
    # path pure (dict lookup + existence check) and defers the corner
    # cases to the existing, tested resolver. The performance win comes
    # from killing the O(|all_paths|) linear scans INSIDE the resolver's
    # helpers, which is what the task-003 legacy edge resolver
    # effectively triggered per reference.
    try:
        resolved = resolve_fn(
            record, repo_root=repo_root, all_paths=known_paths, source_file=source_file
        )
    except Exception:
        # Any resolver-side failure for a single import is not fatal —
        # we simply miss that edge.
        return None

    if resolved is None:
        return None
    target_symbol = getattr(resolved, "target_symbol", None)
    target_path = getattr(resolved, "target_path", None)
    if target_symbol is None or target_path is None:
        # ``import pkg.sub`` style — no symbol-level edge (only module
        # granularity). Planner falls back on file-level invalidation
        # for these cases.
        return None

    target_sid = f"{target_path}::{target_symbol}"
    if target_sid in graph_symbols:
        return target_sid
    return None


def _populate_edges_from_parse_results(
    graph: "CallGraph", parse_results: dict[str, Any]
) -> None:
    """Thin adapter that forwards to :func:`_resolve_edges_v2`.

    Named separately so the class-method body reads as "pass 2 — edge
    resolution" without leaking the specific v2 implementation name.
    """

    _resolve_edges_v2(graph, parse_results, graph._repo_root)


__all__ = [
    "SymbolInfo",
    "CallGraph",
]
