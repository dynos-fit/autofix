"""Tests for ``CallGraph.callees_of`` — mirror of ``callers_of``."""
from __future__ import annotations

import pytest

from autofix.invalidation.call_graph import CallGraph


def _build_graph(edges: list[tuple[str, str]]) -> CallGraph:
    """Construct a CallGraph by directly populating its private dicts.

    Each edge ``(caller, callee)`` adds caller → callee to ``_callees``
    and the symmetric callee → caller to ``_callers`` so the dual-
    direction invariant is preserved (matching production builds).

    Symbols are auto-registered as bare-name entries in ``_symbols``
    (the test only exercises traversal — the SymbolInfo content is
    irrelevant for callers_of/callees_of semantics).
    """
    from autofix.invalidation.call_graph import SymbolInfo

    g = CallGraph()
    seen: set[str] = set()
    for caller, callee in edges:
        for sid in (caller, callee):
            if sid not in seen:
                seen.add(sid)
                g._symbols[sid] = SymbolInfo(
                    symbol_id=sid, kind="function", path="test.py",
                    name=sid, start_line=1, end_line=1,
                )
        g._callees.setdefault(caller, set()).add(callee)
        g._callers.setdefault(callee, set()).add(caller)
    return g


# ---------------------------------------------------------------------------
# Semantics
# ---------------------------------------------------------------------------


def test_max_depth_zero_returns_empty() -> None:
    g = _build_graph([("a", "b"), ("b", "c")])
    assert g.callees_of(["a"], max_depth=0) == frozenset()


def test_max_depth_negative_returns_empty() -> None:
    g = _build_graph([("a", "b")])
    assert g.callees_of(["a"], max_depth=-3) == frozenset()


def test_bare_str_raises_typeerror() -> None:
    g = _build_graph([("a", "b")])
    with pytest.raises(TypeError):
        g.callees_of("a", max_depth=1)  # type: ignore[arg-type]


def test_seeds_never_included_in_result() -> None:
    g = _build_graph([("a", "b"), ("b", "c")])
    result = g.callees_of(["a"], max_depth=10)
    assert "a" not in result


def test_max_depth_one_returns_direct_callees() -> None:
    g = _build_graph([("a", "b"), ("a", "c"), ("b", "d")])
    assert g.callees_of(["a"], max_depth=1) == frozenset({"b", "c"})


def test_max_depth_two_returns_two_hop_callees() -> None:
    g = _build_graph([("a", "b"), ("b", "c"), ("c", "d")])
    assert g.callees_of(["a"], max_depth=2) == frozenset({"b", "c"})
    assert g.callees_of(["a"], max_depth=3) == frozenset({"b", "c", "d"})


def test_cycles_visited_at_most_once() -> None:
    """A → B → C → A must not loop forever."""
    g = _build_graph([("a", "b"), ("b", "c"), ("c", "a")])
    result = g.callees_of(["a"], max_depth=10)
    assert result == frozenset({"b", "c"})


def test_unknown_symbol_id_returns_empty() -> None:
    g = _build_graph([("a", "b")])
    assert g.callees_of(["nonexistent"], max_depth=5) == frozenset()


def test_multiple_seeds_union() -> None:
    g = _build_graph([("a", "b"), ("c", "d")])
    result = g.callees_of(["a", "c"], max_depth=1)
    assert result == frozenset({"b", "d"})


def test_callees_and_callers_are_symmetric_on_inverted_query() -> None:
    """For an edge a→b, callees_of(a) contains b AND callers_of(b)
    contains a. Sanity check that the dual-direction invariant from
    ``callers_of``'s test suite holds for ``callees_of`` too.
    """
    g = _build_graph([("a", "b")])
    assert "b" in g.callees_of(["a"], max_depth=1)
    assert "a" in g.callers_of(["b"], max_depth=1)


def test_returns_frozenset() -> None:
    """Return type must be frozenset (immutable hashable) — caller
    might use it as a dict key or set member.
    """
    g = _build_graph([("a", "b")])
    result = g.callees_of(["a"], max_depth=1)
    assert isinstance(result, frozenset)
