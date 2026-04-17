"""Unit tests for the new ``autofix_next.invalidation.planner``.

Covers:

* AC #11 — ``Invalidation`` dataclass is frozen with exactly five fields
  in order ``(affected_symbols, affected_files, is_full_sweep, depth_used,
  source_changeset)``; ``DEFAULT_CALLER_DEPTH == 3``.
* AC #12 — ``plan()`` signature is ``(graph, changeset, *, max_depth=...)``;
  the old identity stub is gone.
* AC #13 — non-fresh ChangeSet: seeds from ``graph.symbols_in(path)`` plus
  transitive callers up to ``max_depth`` plus the seeds themselves; paths
  pass through as-is.
* AC #14 — fresh-instance ChangeSet: bounded full sweep over graph symbols
  WITHOUT traversing the caller adjacency (verified by seeding a cyclic
  graph and checking that ``plan`` returns without hitting cycles).
* AC #15 — empty non-fresh ChangeSet: empty Invalidation, not a full sweep.
* AC #16 — new-file case: path on disk but not in graph is parsed on the
  fly and its symbols appear in ``affected_symbols``.
* AC #17 — deleted-file case: path not on disk does not raise and still
  appears in ``affected_files``.
* AC #18 — non-``.py`` file case: contributes zero seeds but appears
  verbatim in ``affected_files``.

Tests that cross the parser boundary (``test_plan_new_file_parsed_on_the_fly``)
use ``pytest.importorskip("tree_sitter_python")`` at the call site.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path
from typing import get_type_hints

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_symbol(symbol_id: str, path: str, name: str):
    from autofix_next.invalidation.call_graph import SymbolInfo

    return SymbolInfo(
        symbol_id=symbol_id,
        path=path,
        name=name,
        kind="function",
        start_line=1,
        end_line=5,
    )


def _empty_graph():
    from autofix_next.invalidation.call_graph import CallGraph

    g = CallGraph.__new__(CallGraph)
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
    graph._callees.setdefault(caller, set()).add(callee)
    graph._callers.setdefault(callee, set()).add(caller)


def _make_changeset(paths: tuple[str, ...], *, fresh: bool = False):
    from autofix_next.events.schema import ChangeSet

    return ChangeSet(
        paths=paths,
        watcher_confidence="diff-head1",
        is_fresh_instance=fresh,
    )


# ---------------------------------------------------------------------------
# AC #11 — dataclass shape
# ---------------------------------------------------------------------------


def test_invalidation_dataclass_shape_frozen() -> None:
    """AC #11: ``Invalidation`` is a frozen dataclass with exactly the five
    fields declared in the documented order; ``DEFAULT_CALLER_DEPTH == 3``."""
    from autofix_next.invalidation import planner as planner_mod
    from autofix_next.invalidation.planner import DEFAULT_CALLER_DEPTH, Invalidation

    assert DEFAULT_CALLER_DEPTH == 3

    # Dataclass + frozen.
    assert dataclasses.is_dataclass(Invalidation)
    fields = dataclasses.fields(Invalidation)
    names = [f.name for f in fields]
    assert names == [
        "affected_symbols",
        "affected_files",
        "is_full_sweep",
        "depth_used",
        "source_changeset",
    ], f"field order mismatch: got {names}"

    # Frozen check — attempting to mutate a constructed instance must raise.
    sentinel_changeset = _make_changeset(paths=())
    inst = Invalidation(
        affected_symbols=frozenset(),
        affected_files=(),
        is_full_sweep=False,
        depth_used=3,
        source_changeset=sentinel_changeset,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        inst.is_full_sweep = True  # type: ignore[misc]

    # Module exports (sanity; keeps future wholesale rename honest).
    assert hasattr(planner_mod, "plan")
    assert hasattr(planner_mod, "Invalidation")


# ---------------------------------------------------------------------------
# AC #12 — new plan() signature; old identity stub removed
# ---------------------------------------------------------------------------


def test_plan_signature_replaces_stub() -> None:
    """AC #12: the new ``plan`` takes ``(graph, changeset, *, max_depth=...)``
    and the old identity signature ``plan(changeset) -> ChangeSet`` is gone."""
    from autofix_next.invalidation.planner import plan

    sig = inspect.signature(plan)
    params = sig.parameters
    assert "graph" in params
    assert "changeset" in params
    assert "max_depth" in params
    # max_depth must be keyword-only.
    assert params["max_depth"].kind == inspect.Parameter.KEYWORD_ONLY

    # The old stub returned a ChangeSet; the new plan must NOT accept a
    # single positional ChangeSet. Calling plan(changeset) alone must fail.
    cs = _make_changeset(paths=())
    with pytest.raises(TypeError):
        plan(cs)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# AC #13 — non-fresh changeset: seeds + callers
# ---------------------------------------------------------------------------


def test_plan_nonfresh_seeds_and_callers() -> None:
    """AC #13: non-fresh ChangeSet seeds ``graph.symbols_in(path)`` and
    expands with ``graph.callers_of(seeds, max_depth)``; seeds are unioned
    in; ``affected_files`` is the sorted tuple of all symbol paths plus the
    raw changeset paths."""
    from autofix_next.invalidation.planner import plan

    # Hand-build a 4-symbol graph with 2 edges (2 cross-file callers).
    g = _empty_graph()
    s_b = _make_symbol("b.py::b_func", "b.py", "b_func")
    s_a = _make_symbol("a.py::a_func", "a.py", "a_func")
    s_c = _make_symbol("c.py::c_func", "c.py", "c_func")
    s_d = _make_symbol("d.py::d_func", "d.py", "d_func")
    for s in (s_b, s_a, s_c, s_d):
        _add_symbol(g, s)
    # Edges: a calls b; c calls a. d is unrelated.
    _add_edge(g, s_a.symbol_id, s_b.symbol_id)
    _add_edge(g, s_c.symbol_id, s_a.symbol_id)

    changeset = _make_changeset(paths=("b.py",))
    result = plan(g, changeset, max_depth=3)

    # b_func is the seed; a_func and c_func are in its upward cone.
    assert result.affected_symbols == frozenset(
        {s_b.symbol_id, s_a.symbol_id, s_c.symbol_id}
    )
    # affected_files is a sorted tuple of all impacted paths + the changeset
    # path ("b.py" which is already in the symbol-path set).
    assert result.affected_files == ("a.py", "b.py", "c.py")
    assert result.is_full_sweep is False
    assert result.depth_used == 3
    assert result.source_changeset is changeset


# ---------------------------------------------------------------------------
# AC #14 — fresh-instance bounded full sweep, graph NOT traversed
# ---------------------------------------------------------------------------


def test_plan_fresh_instance_returns_bounded_full_sweep() -> None:
    """AC #14: a fresh-instance ChangeSet returns a bounded full sweep over
    ``graph.all_symbols`` / ``graph.all_paths`` WITHOUT walking the caller
    graph. Verified by installing a cycle that would otherwise hang a BFS
    visitor that lacked a ``visited`` set — the fast path must not traverse."""
    from autofix_next.invalidation.planner import plan

    # Build a tiny cyclic graph. The fresh-instance path must return based
    # on ``all_symbols`` / ``all_paths`` alone; a traversal-based path would
    # still terminate thanks to the BFS visited set, but the point of AC #14
    # is that traversal does not happen at all.
    g = _empty_graph()
    a = _make_symbol("p.py::A", "p.py", "A")
    b = _make_symbol("p.py::B", "p.py", "B")
    _add_symbol(g, a)
    _add_symbol(g, b)
    _add_edge(g, a.symbol_id, b.symbol_id)
    _add_edge(g, b.symbol_id, a.symbol_id)  # cycle

    # Sentinel: replace callers_of with a raising stub. If the planner's
    # fresh-instance fast path erroneously invokes it, this test fails.
    def _callers_of_must_not_run(*args, **kwargs):  # pragma: no cover
        raise AssertionError(
            "plan() must NOT call callers_of() on the fresh-instance path"
        )

    g.callers_of = _callers_of_must_not_run  # type: ignore[method-assign]

    changeset = _make_changeset(paths=(), fresh=True)
    result = plan(g, changeset, max_depth=3)

    assert result.is_full_sweep is True
    assert result.affected_symbols == frozenset({a.symbol_id, b.symbol_id})
    assert result.affected_files == ("p.py",)
    assert result.depth_used == 3
    assert result.source_changeset is changeset


# ---------------------------------------------------------------------------
# AC #15 — empty non-fresh changeset
# ---------------------------------------------------------------------------


def test_plan_empty_nonfresh_is_empty_not_sweep() -> None:
    """AC #15: ``paths=()`` + ``is_fresh_instance=False`` returns empty
    ``affected_symbols``, empty ``affected_files``, ``is_full_sweep=False``."""
    from autofix_next.invalidation.planner import plan

    g = _empty_graph()
    _add_symbol(g, _make_symbol("x.py::X", "x.py", "X"))
    changeset = _make_changeset(paths=(), fresh=False)
    result = plan(g, changeset)

    assert result.affected_symbols == frozenset()
    assert result.affected_files == ()
    assert result.is_full_sweep is False


# ---------------------------------------------------------------------------
# AC #16 — new file parsed on the fly
# ---------------------------------------------------------------------------


def test_plan_new_file_parsed_on_the_fly(tmp_path: Path) -> None:
    """AC #16: a ChangeSet path that exists on disk but is not yet in the
    graph is parsed on the fly; its top-level symbols appear in
    ``affected_symbols``; the graph is not mutated."""
    pytest.importorskip("tree_sitter_python")

    from autofix_next.invalidation.planner import plan

    # Build a graph that has one symbol in an already-known file. The
    # ChangeSet includes a new file on disk that's NOT in the graph.
    g = _empty_graph()
    known = _make_symbol("known.py::already", "known.py", "already")
    _add_symbol(g, known)

    new_rel = "newly_added.py"
    (tmp_path / new_rel).write_text(
        "def brand_new():\n    return 42\n\nclass NewClass:\n    pass\n",
        encoding="utf-8",
    )
    # Store tmp_path on the graph so the planner knows the repo_root for
    # on-the-fly parse. (If the implementation uses a different mechanism,
    # the test will guide it toward exposing this.)
    g._repo_root = tmp_path  # type: ignore[attr-defined]

    changeset = _make_changeset(paths=(new_rel,))
    result = plan(g, changeset)

    # Expect the new file's symbols to appear.
    affected_names = {sid.split("::", 1)[1] for sid in result.affected_symbols}
    assert "brand_new" in affected_names
    assert "NewClass" in affected_names
    assert new_rel in result.affected_files

    # Graph must not have been mutated to include the new file.
    assert "newly_added.py::brand_new" not in g._symbols
    assert "newly_added.py" not in g._path_to_symbols


# ---------------------------------------------------------------------------
# AC #17 — deleted file path: no raise, path still in affected_files
# ---------------------------------------------------------------------------


def test_plan_deleted_file_no_raise(tmp_path: Path) -> None:
    """AC #17: a ChangeSet path that does not exist on disk does not cause
    a raise; any previously-known symbols for that path are included as
    seeds; the path appears in ``affected_files``."""
    from autofix_next.invalidation.planner import plan

    g = _empty_graph()
    # Pretend the graph was built before the delete.
    ghost = _make_symbol("gone.py::ghost", "gone.py", "ghost")
    caller = _make_symbol("other.py::caller_fn", "other.py", "caller_fn")
    _add_symbol(g, ghost)
    _add_symbol(g, caller)
    _add_edge(g, caller.symbol_id, ghost.symbol_id)
    g._repo_root = tmp_path  # type: ignore[attr-defined]

    changeset = _make_changeset(paths=("gone.py",))
    # No file at (tmp_path / "gone.py"). Planner must NOT raise.
    result = plan(g, changeset)

    assert ghost.symbol_id in result.affected_symbols
    assert caller.symbol_id in result.affected_symbols
    assert "gone.py" in result.affected_files


# ---------------------------------------------------------------------------
# AC #18 — non-py files pass through without seeding
# ---------------------------------------------------------------------------


def test_plan_non_py_file_passthrough(tmp_path: Path) -> None:
    """AC #18: paths that do not end in ``.py`` produce zero seeds but
    appear verbatim in ``affected_files``."""
    from autofix_next.invalidation.planner import plan

    g = _empty_graph()
    _add_symbol(g, _make_symbol("x.py::X", "x.py", "X"))
    g._repo_root = tmp_path  # type: ignore[attr-defined]

    changeset = _make_changeset(paths=("README.md", "data.json"))
    result = plan(g, changeset)

    assert result.affected_symbols == frozenset()
    assert "README.md" in result.affected_files
    assert "data.json" in result.affected_files
    assert result.is_full_sweep is False
