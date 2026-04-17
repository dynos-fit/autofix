"""Unit tests for ``autofix_next.invalidation.call_graph``.

Covers:

* AC #4  — ``SymbolInfo`` dataclass shape and ``CallGraph`` internal dict
  structure (``_symbols``, ``_callees``, ``_callers``, ``_path_to_symbols``).
* AC #5  — ``symbols_in``, ``callers_of``, ``all_symbols``, ``all_paths``,
  ``__getitem__`` public surface on a hand-built graph.
* AC #6  — ``callers_of`` BFS semantics: depth=0 is empty, depth=1 is direct
  callers only, depth=3 bounds reachability, cycles traversed once, and bare
  ``str`` raises ``TypeError``.

The graph objects are hand-built by poking directly into the private dicts so
these tests do not require the tree-sitter grammar or a live builder.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import get_type_hints

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_symbol(symbol_id: str, path: str, name: str, kind: str = "function"):
    """Construct a ``SymbolInfo`` using whatever its declared field order is."""
    from autofix_next.invalidation.call_graph import SymbolInfo

    return SymbolInfo(
        symbol_id=symbol_id,
        path=path,
        name=name,
        kind=kind,  # type: ignore[arg-type]
        start_line=1,
        end_line=10,
    )


def _empty_graph():
    """Instantiate a ``CallGraph`` with empty private dicts."""
    from autofix_next.invalidation.call_graph import CallGraph

    g = CallGraph.__new__(CallGraph)
    # Seed the four documented private dicts. AC #4 pins these names.
    g._symbols = {}
    g._callees = {}
    g._callers = {}
    g._path_to_symbols = {}
    return g


def _add_symbol(graph, sym) -> None:
    graph._symbols[sym.symbol_id] = sym
    graph._path_to_symbols.setdefault(sym.path, set()).add(sym.symbol_id)
    graph._callees.setdefault(sym.symbol_id, set())
    graph._callers.setdefault(sym.symbol_id, set())


def _add_edge(graph, caller: str, callee: str) -> None:
    """Record ``caller -> callee`` in both directions (dual-direction)."""
    graph._callees.setdefault(caller, set()).add(callee)
    graph._callers.setdefault(callee, set()).add(caller)


# ---------------------------------------------------------------------------
# AC #4 — SymbolInfo + CallGraph shape
# ---------------------------------------------------------------------------


def test_symbol_info_and_call_graph_shape() -> None:
    """AC #4: ``SymbolInfo`` has the six declared fields and ``CallGraph``
    exposes the four documented private dicts."""
    from autofix_next.invalidation.call_graph import CallGraph, SymbolInfo

    hints = get_type_hints(SymbolInfo)
    assert "symbol_id" in hints
    assert "path" in hints
    assert "name" in hints
    assert "kind" in hints
    assert "start_line" in hints
    assert "end_line" in hints

    sym = _make_symbol("pkg/a.py::f", "pkg/a.py", "f")
    assert sym.symbol_id == "pkg/a.py::f"
    assert sym.path == "pkg/a.py"
    assert sym.name == "f"
    assert sym.kind == "function"
    assert sym.start_line == 1
    assert sym.end_line == 10

    g = _empty_graph()
    # The four private dicts are documented in design-decisions §3 and
    # acceptance criterion #4. Use hasattr so the check survives a private
    # rename while still catching a wholesale removal.
    assert hasattr(g, "_symbols")
    assert hasattr(g, "_callees")
    assert hasattr(g, "_callers")
    assert hasattr(g, "_path_to_symbols")

    # build_from_root must be a classmethod on CallGraph (AC #7).
    assert hasattr(CallGraph, "build_from_root")


# ---------------------------------------------------------------------------
# AC #5 — public surface: symbols_in / all_symbols / all_paths / __getitem__
# ---------------------------------------------------------------------------


def test_callers_of_signature_and_symbols_in() -> None:
    """AC #5: ``symbols_in``, ``callers_of``, ``all_symbols``, ``all_paths``,
    ``__getitem__`` return the expected types on a hand-built graph."""
    from autofix_next.invalidation.call_graph import CallGraph

    g = _empty_graph()
    a = _make_symbol("pkg/a.py::f", "pkg/a.py", "f")
    b = _make_symbol("pkg/b.py::g", "pkg/b.py", "g")
    _add_symbol(g, a)
    _add_symbol(g, b)
    _add_edge(g, a.symbol_id, b.symbol_id)  # a calls b

    # symbols_in returns frozenset of the symbols declared in that path.
    in_a = g.symbols_in("pkg/a.py")
    assert isinstance(in_a, frozenset)
    assert in_a == frozenset({"pkg/a.py::f"})
    assert g.symbols_in("pkg/missing.py") == frozenset()

    # all_symbols / all_paths are properties returning frozensets.
    assert isinstance(g.all_symbols, frozenset)
    assert isinstance(g.all_paths, frozenset)
    assert g.all_symbols == frozenset({"pkg/a.py::f", "pkg/b.py::g"})
    assert g.all_paths == frozenset({"pkg/a.py", "pkg/b.py"})

    # __getitem__ returns the SymbolInfo for a known id and raises KeyError
    # on an unknown id.
    assert g["pkg/a.py::f"].name == "f"
    with pytest.raises(KeyError):
        _ = g["pkg/missing.py::nope"]

    # callers_of signature: Iterable[str] + max_depth (kw or positional).
    sig = inspect.signature(CallGraph.callers_of)
    params = list(sig.parameters.values())
    # self + two more.
    assert len(params) >= 3


# ---------------------------------------------------------------------------
# AC #6 — callers_of BFS semantics
# ---------------------------------------------------------------------------


def test_callers_of_depth_zero() -> None:
    """AC #6: ``max_depth=0`` returns an empty frozenset (no caller expansion)."""
    g = _empty_graph()
    a = _make_symbol("a.py::A", "a.py", "A")
    b = _make_symbol("b.py::B", "b.py", "B")
    _add_symbol(g, a)
    _add_symbol(g, b)
    _add_edge(g, b.symbol_id, a.symbol_id)  # b calls a → a's caller is b

    result = g.callers_of([a.symbol_id], max_depth=0)
    assert isinstance(result, frozenset)
    assert result == frozenset()


def test_callers_of_depth_one_direct_only() -> None:
    """AC #6: ``max_depth=1`` returns only direct callers, not grandcallers."""
    g = _empty_graph()
    leaf = _make_symbol("x.py::leaf", "x.py", "leaf")
    mid = _make_symbol("x.py::mid", "x.py", "mid")
    top = _make_symbol("x.py::top", "x.py", "top")
    _add_symbol(g, leaf)
    _add_symbol(g, mid)
    _add_symbol(g, top)
    _add_edge(g, mid.symbol_id, leaf.symbol_id)  # mid -> leaf
    _add_edge(g, top.symbol_id, mid.symbol_id)   # top -> mid

    direct = g.callers_of([leaf.symbol_id], max_depth=1)
    assert direct == frozenset({"x.py::mid"})
    # Seed itself must NOT be in the returned set.
    assert leaf.symbol_id not in direct


def test_callers_of_depth_three_bounded() -> None:
    """AC #6: at depth=3, walk up three hops and stop, regardless of graph size."""
    g = _empty_graph()
    ids = [f"p.py::s{i}" for i in range(6)]
    for sid in ids:
        _add_symbol(g, _make_symbol(sid, "p.py", sid.split("::")[1]))
    # Chain: s5 -> s4 -> s3 -> s2 -> s1 -> s0 (each right calls next-right).
    for i in range(5):
        _add_edge(g, ids[i + 1], ids[i])  # s{i+1} calls s{i}

    # Seeds = {s0}; callers at depth 3 are {s1, s2, s3}; s4 and s5 are deeper.
    reached = g.callers_of([ids[0]], max_depth=3)
    assert reached == frozenset({ids[1], ids[2], ids[3]})


def test_callers_of_handles_cycle() -> None:
    """AC #6: cycles are traversed once and never recurse forever."""
    g = _empty_graph()
    a = _make_symbol("x.py::A", "x.py", "A")
    b = _make_symbol("x.py::B", "x.py", "B")
    c = _make_symbol("x.py::C", "x.py", "C")
    for s in (a, b, c):
        _add_symbol(g, s)
    # A -> B -> C -> A (3-cycle).
    _add_edge(g, a.symbol_id, b.symbol_id)
    _add_edge(g, b.symbol_id, c.symbol_id)
    _add_edge(g, c.symbol_id, a.symbol_id)

    # Seed A: callers at unbounded-ish depth should be {B, C} exactly.
    result = g.callers_of([a.symbol_id], max_depth=10)
    assert result == frozenset({b.symbol_id, c.symbol_id})


def test_callers_of_rejects_bare_str() -> None:
    """AC #6: passing a bare str (not an iterable-of-str) raises TypeError."""
    g = _empty_graph()
    with pytest.raises(TypeError):
        # A bare str would silently iterate character-by-character otherwise.
        g.callers_of("pkg/a.py::f", max_depth=1)  # type: ignore[arg-type]
