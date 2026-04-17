"""Incremental invalidation planner (AC #11 / #12 / #13 / #14 / #15 / #16 / #17 / #18).

Computes an :class:`Invalidation` from a :class:`CallGraph` + :class:`ChangeSet`.

Two execution modes:

* **Fresh-instance fast path** — when ``ChangeSet.is_fresh_instance`` is True
  the planner downgrades to a bounded full sweep over the graph's known
  symbols only. It does **not** traverse the caller adjacency and it does
  **not** touch the filesystem. The ``InvalidationComputed`` telemetry row
  can record ``graph_symbol_count == len(all_symbols)`` so operators can
  distinguish "graph was empty" from "planner skipped".
* **Incremental path** — seeds ``graph.symbols_in(path)`` for each path in
  the changeset, parses any new-on-disk path on the fly (without mutating
  the shared :class:`CallGraph`), then expands with
  ``graph.callers_of(seeds, max_depth)`` and unions the seeds back in.

An empty non-fresh ``ChangeSet`` returns an empty ``Invalidation`` — the
planner never silently upgrades "nothing changed" to "sweep everything"
(AC #15).

Non-``.py`` files (AC #18) and paths that no longer exist on disk
(AC #17) pass through verbatim into ``affected_files`` even though they
contribute no new seeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autofix_next.events.schema import ChangeSet
from autofix_next.invalidation.call_graph import CallGraph

DEFAULT_CALLER_DEPTH: int = 3


@dataclass(slots=True, frozen=True)
class Invalidation:
    """Immutable result of an invalidation plan (AC #11).

    Field order is part of the public contract and pinned by the TDD test
    suite — do not reorder without updating the consumers in seg-5.

    Attributes
    ----------
    affected_symbols:
        Set of ``symbol_id`` values that need re-scanning. Seeds plus
        transitive callers (up to ``depth_used``) on the incremental path,
        or ``graph.all_symbols`` on the fresh-instance path.
    affected_files:
        Sorted tuple of repo-relative paths. Always includes every raw
        path from the source :class:`ChangeSet` (even non-``.py`` and
        deleted paths) unioned with the paths hosting any affected symbol.
    is_full_sweep:
        ``True`` iff this plan came from the fresh-instance fast path.
        An empty non-fresh changeset is NOT a full sweep.
    depth_used:
        The ``max_depth`` value the caller requested. Recorded so the
        telemetry row is self-describing.
    source_changeset:
        The exact :class:`ChangeSet` instance the plan was built from.
        Stored by reference so downstream consumers can correlate
        invalidations with the upstream event without re-hashing.
    """

    affected_symbols: frozenset[str]
    affected_files: tuple[str, ...]
    is_full_sweep: bool
    depth_used: int
    source_changeset: ChangeSet


def _graph_root(graph: CallGraph) -> Path | None:
    """Return the on-disk root the graph was built from, if any.

    Prefers the ``_repo_root`` slot (populated by both
    :meth:`CallGraph.build_from_root` and by hand-built test graphs) and
    falls back to ``_root`` for defensive compatibility. Either slot being
    absent is treated as "no known root" — not an error.
    """

    root = getattr(graph, "_repo_root", None)
    if root is None:
        root = getattr(graph, "_root", None)
    return root


def _parse_new_file_symbols(
    root: Path, relpath: str
) -> frozenset[str]:
    """Parse a file not yet in the graph; return its top-level symbol IDs.

    Does **not** mutate the caller's :class:`CallGraph` — the parsed
    symbols are only used to seed the local invalidation plan. Any parse
    failure (grammar not available, IO error, malformed source) collapses
    to an empty frozenset so the planner never raises mid-flight.
    """

    # Lazy imports — the parser module can be missing when the grammar
    # wheel isn't installed, and the symbol extractor is module-private to
    # call_graph. Pulling them in only when we need them keeps the happy
    # path (known files only) grammar-free.
    try:
        from autofix_next.parsing.tree_sitter import (
            TreeSitterLoadError,
            parse_file,
        )
        from autofix_next.invalidation.call_graph import _extract_symbols
    except ImportError:
        # tree-sitter wheel not installed, or helper not yet available.
        # Treat as "no seeds" rather than raise.
        return frozenset()

    abs_path = root / relpath
    try:
        pr = parse_file(abs_path, repo_root=root)
    except TreeSitterLoadError:
        return frozenset()
    except (FileNotFoundError, OSError):
        return frozenset()
    # Tree-sitter still returns a tree on malformed source; defensive
    # check on the ``has_error`` attribute matches the pass-1 guard in
    # ``CallGraph.build_from_root``.
    if getattr(pr.tree.root_node, "has_error", False):
        return frozenset()
    try:
        syms = _extract_symbols(pr, relpath)
    except Exception:
        # A broken tree-sitter subtree should not abort the planner —
        # we degrade to "no new seeds" for this file only.
        return frozenset()
    return frozenset(s.symbol_id for s in syms)


def plan(
    graph: CallGraph,
    changeset: ChangeSet,
    *,
    max_depth: int = DEFAULT_CALLER_DEPTH,
) -> Invalidation:
    """Compute the :class:`Invalidation` plan for ``changeset`` (AC #12).

    Parameters
    ----------
    graph:
        The current :class:`CallGraph`. Read-only — ``plan`` never mutates
        the graph, even when parsing a brand-new on-disk file.
    changeset:
        The :class:`ChangeSet` describing the paths that changed. A
        ``is_fresh_instance=True`` flag triggers the full-sweep fast path.
    max_depth:
        Keyword-only. How many levels of transitive callers to expand on
        the incremental path. Defaults to :data:`DEFAULT_CALLER_DEPTH`.

    Returns
    -------
    Invalidation
        Frozen plan. Never ``None``; never raises for missing files
        (AC #17) or non-``.py`` paths (AC #18).
    """

    # -- Fresh-instance fast path (AC #14) -------------------------------
    # The caller graph is NOT traversed here: we use the graph's own
    # materialized ``all_symbols`` / ``all_paths`` accessors, so cycles in
    # ``_callers`` are irrelevant and the filesystem is never scanned.
    if changeset.is_fresh_instance:
        return Invalidation(
            affected_symbols=graph.all_symbols,
            affected_files=tuple(sorted(graph.all_paths)),
            is_full_sweep=True,
            depth_used=max_depth,
            source_changeset=changeset,
        )

    # -- Empty non-fresh ChangeSet (AC #15) -----------------------------
    # Never silently upgrade to a full sweep — an empty changeset means
    # "nothing to do" and the planner respects that.
    if not changeset.paths:
        return Invalidation(
            affected_symbols=frozenset(),
            affected_files=(),
            is_full_sweep=False,
            depth_used=max_depth,
            source_changeset=changeset,
        )

    # -- Incremental path (AC #13 / #16 / #17 / #18) --------------------
    root = _graph_root(graph)

    seeds: set[str] = set()
    # ``local_paths_by_sid`` lets us recover the host path of a seed that
    # came from an on-the-fly parse (those symbols are not in graph._symbols,
    # so ``graph[sid].path`` would raise KeyError).
    local_paths_by_sid: dict[str, str] = {}

    for relpath in changeset.paths:
        known = graph.symbols_in(relpath)
        if known:
            # Known file — take all its symbols as seeds (AC #13).
            # Deleted-file case (AC #17) also lands here when the graph
            # still remembers the previously-indexed symbols.
            seeds.update(known)
            continue
        # Path not in graph: new file on disk, deleted file, or non-py.
        # Only try on-the-fly parsing for ``.py`` files that actually
        # exist on disk. Non-py files (AC #18) and deleted files (AC #17)
        # both silently pass through with zero new seeds and still appear
        # in ``affected_files`` below.
        if root is not None and relpath.endswith(".py"):
            abs_path = root / relpath
            try:
                exists = abs_path.is_file()
            except OSError:
                # Filesystem error probing the path — treat as missing.
                exists = False
            if exists:
                new_sids = _parse_new_file_symbols(root, relpath)
                for sid in new_sids:
                    seeds.add(sid)
                    local_paths_by_sid[sid] = relpath

    # -- Transitive caller expansion (AC #13) ---------------------------
    # ``callers_of`` returns just the upward-reachable callers, excluding
    # the seeds themselves. Per AC #13 we union the seeds back in.
    caller_ids = (
        graph.callers_of(seeds, max_depth) if seeds else frozenset()
    )
    affected_symbols = frozenset(seeds | caller_ids)

    # -- Host-path collection (AC #13 / #16 / #17 / #18) ---------------
    # Every affected symbol contributes its host path; the raw changeset
    # paths are unioned in verbatim so non-py (AC #18) and deleted paths
    # (AC #17) pass through even though they have no indexed symbols.
    host_paths: set[str] = set()
    for sid in affected_symbols:
        try:
            host_paths.add(graph[sid].path)
        except KeyError:
            # Symbol came from an on-the-fly parse (AC #16) — it is not
            # in graph._symbols. Recover the path from our local map.
            local = local_paths_by_sid.get(sid)
            if local is not None:
                host_paths.add(local)
            elif "::" in sid:
                # Fallback: ``symbol_id`` is ``<path>::<qualified-name>``.
                # This branch is defensive only; seeds from on-the-fly
                # parses always go through ``local_paths_by_sid`` first.
                host_paths.add(sid.rsplit("::", 1)[0])

    affected_files = tuple(sorted(host_paths | set(changeset.paths)))

    return Invalidation(
        affected_symbols=affected_symbols,
        affected_files=affected_files,
        is_full_sweep=False,
        depth_used=max_depth,
        source_changeset=changeset,
    )


__all__ = [
    "DEFAULT_CALLER_DEPTH",
    "Invalidation",
    "plan",
]
