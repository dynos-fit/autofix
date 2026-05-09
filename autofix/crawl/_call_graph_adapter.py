"""Path-level wrapper around the symbol-level ``CallGraph``.

The crawler's ``expand_bundle`` consumes a
:class:`autofix.crawl.contracts.CallGraphAdapter` whose contract is
``neighbors_of(path: Path) -> list[Path]`` — file-level granularity.

The project's ``autofix.invalidation.call_graph.CallGraph`` exposes
symbol-level traversal: ``symbols_in(path)``, ``callers_of(symbol_ids,
max_depth)``, ``callees_of(symbol_ids, max_depth)``. This module
bridges the two, so the crawler can stay subsystem-pure (it never
imports ``autofix.invalidation`` directly) while still consuming
the real call graph at runtime via the adapter pattern.

The adapter is wired in
``autofix/cli/cycle_runner.py::_build_call_graph`` — that's the
only consumer who knows about both halves of the bridge. The
crawler imports ``CallGraphAdapter`` from ``contracts`` and treats
this concrete class as opaque.

Internal name (leading underscore on the module) — operators of the
crawler should declare the Protocol type, not import this class
directly. The Protocol is the supported surface; this is the
implementation detail satisfying it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class CallGraphPathAdapter:
    """Wrap a symbol-level ``CallGraph`` as a path-level adapter.

    Satisfies the :class:`autofix.crawl.contracts.CallGraphAdapter`
    Protocol at runtime.

    For a query path P:

    1. Look up the symbols declared in P via
       ``call_graph.symbols_in(str(P))``.
    2. If there are no symbols, return [] (the file isn't in the
       graph — could be a file the symbol scanner doesn't index,
       or a brand-new file).
    3. Compute one hop in each direction:
       ``callers = callers_of(symbols, max_depth=1)``,
       ``callees = callees_of(symbols, max_depth=1)``.
    4. Take the union, then subtract the seed's own symbols (in
       case of intra-file edges that would otherwise show the
       seed's own path as a neighbor).
    5. Map each remaining ``symbol_id`` back to a relpath using
       the documented format ``"<relpath>::<qualified-name>"``
       (split on ``::``, take the prefix).
    6. Dedup, sort, return as ``list[Path]``.

    The class is structural (no inheritance, just method shapes)
    — any object with ``symbols_in``, ``callers_of``, ``callees_of``
    can be wrapped, including test mocks.
    """

    def __init__(self, call_graph: Any) -> None:
        self._cg = call_graph

    def neighbors_of(self, path: Path) -> list[Path]:
        relpath = str(path)
        symbols = self._cg.symbols_in(relpath)
        if not symbols:
            return []

        # One hop in each direction.
        callers = self._cg.callers_of(symbols, max_depth=1)
        callees = self._cg.callees_of(symbols, max_depth=1)

        # Union, minus the seed's own symbols (defensive — most
        # symbol_ids are inter-file but a self-call edge could
        # theoretically loop the seed back as its own neighbor).
        neighbor_symbols = (callers | callees) - frozenset(symbols)

        # symbol_id format: "<relpath>::<qualified-name>".
        # Split, take the prefix, dedup, sort for determinism.
        neighbor_paths: set[str] = set()
        for sid in neighbor_symbols:
            if "::" not in sid:
                # Defensive — malformed symbol_id. Skip rather
                # than crash the cycle.
                continue
            neighbor_relpath = sid.split("::", 1)[0]
            if neighbor_relpath != relpath:
                neighbor_paths.add(neighbor_relpath)

        return [Path(p) for p in sorted(neighbor_paths)]


__all__ = ["CallGraphPathAdapter"]
