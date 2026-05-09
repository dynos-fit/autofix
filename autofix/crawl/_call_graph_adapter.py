"""Path-level wrapper around the symbol-level ``CallGraph``.

The crawler's ``expand_bundle`` consumes a
:class:`autofix.crawl.contracts.CallGraphAdapter` whose contract is
``neighbors_of(path: Path) -> list[Path]`` — file-level granularity.

The project's ``autofix.invalidation.call_graph.CallGraph`` exposes
symbol-level traversal: ``symbols_in(path)``, ``callers_of(symbol_ids,
max_depth)``, ``callees_of(symbol_ids, max_depth)``. This module
bridges the two so the crawler can stay subsystem-pure (it never
imports ``autofix.invalidation`` directly) while still consuming
the real call graph at runtime via the adapter pattern.

The adapter merges two signals when computing neighbors:

1. **SCIP-based** — when a SCIP-style ``call_graph`` is supplied,
   look up the seed file's symbols, then take 1-hop callers ∪
   callees and project back to file paths. Precise, but only
   covers languages that have a SCIP indexer wired in.
2. **Text-reference-based** — when text-reference indexes are
   supplied, look up incoming references (other files mentioning
   this file's basename) and outgoing references (this file
   mentioning other files' basenames). Fuzzy but language-
   agnostic. See :mod:`autofix.crawl._text_reference_index`.

Both are optional and additive. The adapter unions whichever is
present and dedups the result. With neither, ``neighbors_of`` is
constant-empty (effectively a no-op adapter — bundles degrade to
singletons).

Path normalization
------------------

Production callers (the picker / bundle expander) pass *absolute*
paths to ``neighbors_of``; the existing tests pass *relative*
paths. SCIP indexes by relative-path strings, so the adapter
normalizes input to relative for its lookups, then converts the
result back to absolute when ``root`` is provided AND the input
was absolute.

If ``root`` is ``None``, paths flow through unchanged — relative
in, relative out (the test convention).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class CallGraphPathAdapter:
    """Wrap a symbol-level ``CallGraph`` as a path-level adapter.

    Satisfies the :class:`autofix.crawl.contracts.CallGraphAdapter`
    Protocol at runtime.

    Args:
        call_graph: object with ``symbols_in``, ``callers_of``,
            ``callees_of`` methods. ``None`` disables SCIP-based
            neighbors entirely (text-only mode).
        root: repo root used to normalize between absolute and
            relative paths. When ``None``, paths flow through
            unchanged.
        text_incoming: ``{basename: frozenset(relpath_strings)}`` —
            files whose content mentions ``basename``. ``None``
            disables text-incoming neighbors.
        text_outgoing: ``{relpath: frozenset(relpath_strings)}`` —
            files mentioned by the relpath's content. ``None``
            disables text-outgoing neighbors.

    The class is structural — any object with ``symbols_in``,
    ``callers_of``, ``callees_of`` can be wrapped, including test
    mocks.
    """

    def __init__(
        self,
        call_graph: Any | None = None,
        *,
        root: Path | None = None,
        text_incoming: dict[str, frozenset[str]] | None = None,
        text_outgoing: dict[str, frozenset[str]] | None = None,
    ) -> None:
        self._cg = call_graph
        # Resolve root so a relative ``Path('.')`` matches absolute
        # input paths via ``.relative_to``. Without this, callers who
        # pass an unresolved root + absolute paths see SCIP silently
        # return 0 neighbors.
        self._root = root.resolve() if root is not None else None
        self._text_incoming = text_incoming or {}
        self._text_outgoing = text_outgoing or {}

    def neighbors_of(self, path: Path) -> list[Path]:
        rel_str = self._to_rel_str(path)
        scip_rels = self._scip_neighbor_relpaths(rel_str)
        text_rels = self._text_neighbor_relpaths(rel_str, basename=path.name)
        rels = scip_rels | text_rels
        rels.discard(rel_str)

        # Output format mirrors input format: absolute in → absolute
        # out (when ``root`` is set), relative in → relative out.
        # Without this, the picker's ``cand == seed_path`` comparison
        # in expand_bundle would silently fail on every neighbor.
        if path.is_absolute() and self._root is not None:
            return sorted(self._root / r for r in rels)
        return sorted(Path(r) for r in rels)

    def _to_rel_str(self, path: Path) -> str:
        if path.is_absolute() and self._root is not None:
            try:
                return str(path.relative_to(self._root))
            except ValueError:
                # Path is absolute but outside root — best-effort:
                # use the absolute string. SCIP won't find it, but
                # the lookup safely returns []  rather than raising.
                return str(path)
        return str(path)

    def _scip_neighbor_relpaths(self, rel_str: str) -> set[str]:
        if self._cg is None:
            return set()
        symbols = self._cg.symbols_in(rel_str)
        if not symbols:
            return set()
        callers = self._cg.callers_of(symbols, max_depth=1)
        callees = self._cg.callees_of(symbols, max_depth=1)
        neighbor_symbols = (callers | callees) - frozenset(symbols)
        result: set[str] = set()
        for sid in neighbor_symbols:
            if "::" not in sid:
                # Defensive — malformed symbol_id. Skip rather
                # than crash the cycle.
                continue
            n_rel = sid.split("::", 1)[0]
            if n_rel != rel_str:
                result.add(n_rel)
        return result

    def _text_neighbor_relpaths(
        self, rel_str: str, *, basename: str
    ) -> set[str]:
        result: set[str] = set()
        # Files whose content mentions THIS file's basename.
        result.update(self._text_incoming.get(basename, frozenset()))
        # Files THIS file's content mentions, keyed by my relpath.
        result.update(self._text_outgoing.get(rel_str, frozenset()))
        return result


__all__ = ["CallGraphPathAdapter"]
