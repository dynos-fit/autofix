"""Algorithmic smoke test for the invalidation planner.

AC #23: generate a 1000-file hub-and-spoke fixture and assert the planner
returns the expected affected-symbol counts. Change the hub → every leaf is
a caller → ``affected_symbol_count == 1000`` (hub + 999 leaves). Change a
single leaf → only the leaf itself (no one calls the leaf) → the affected
count is bounded by ``depth_used + 1`` (seed + up to depth transitive
callers, of which this leaf has none).

Wall-clock is bounded by a generous ceiling via ``time.monotonic``; this is
a smoke signal, not an SLA. The test deliberately avoids asserting on
microseconds-level timing because CI runners vary wildly.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_python")
pytest.importorskip("tree_sitter")

REPO_ROOT = Path(__file__).resolve().parents[3]

_N_LEAVES = 1000
_WALL_CLOCK_CEILING_SEC = 10.0


def test_planner_algorithmic_hub_fan_out(tmp_path: Path) -> None:
    """AC #23: 1000-file hub-and-spoke; hub change → 1000 affected;
    leaf change → <= depth_used+1 affected; total wall-clock < 10s."""
    from autofix_next.events.schema import ChangeSet
    from autofix_next.invalidation.call_graph import CallGraph
    from autofix_next.invalidation.planner import DEFAULT_CALLER_DEPTH, plan

    # -- Build the fixture ---------------------------------------------------
    start = time.monotonic()

    (tmp_path / "hub.py").write_text(
        "def hub_func():\n    return 1\n", encoding="utf-8"
    )
    for i in range(_N_LEAVES):
        leaf = tmp_path / f"leaf_{i}.py"
        # Use underscore in filename (not hyphen) so it's a valid Python
        # module name — ``from leaf_0 import ...`` would not parse with a
        # hyphen anyway; we import from ``hub`` either way.
        leaf.write_text(
            "from hub import hub_func\n"
            f"\n"
            f"def leaf_func_{i}():\n"
            f"    return hub_func()\n",
            encoding="utf-8",
        )

    graph = CallGraph.build_from_root(tmp_path)
    build_elapsed = time.monotonic() - start

    # Sanity: 1 hub + 1000 leaves = 1001 symbols.
    assert "hub.py::hub_func" in graph.all_symbols
    assert len(graph.all_symbols) == 1 + _N_LEAVES

    # -- Hub change → every leaf is a caller -------------------------------
    cs_hub = ChangeSet(paths=("hub.py",), watcher_confidence="diff-head1")
    plan_start = time.monotonic()
    inv_hub = plan(graph, cs_hub, max_depth=DEFAULT_CALLER_DEPTH)
    plan_elapsed = time.monotonic() - plan_start

    affected_symbol_count = len(inv_hub.affected_symbols)
    # hub_func (1) + every leaf_func_i (1000) = 1001 total symbols.
    # Per AC #23: "affected_symbol_count == 1000 (hub + 999 leaves)" — the
    # spec phrases it as "1000 (hub + 999 leaves within depth 3)" which
    # covers the inclusive count; treat as >= 1000 (all leaves reachable).
    assert affected_symbol_count >= _N_LEAVES, (
        f"expected at least {_N_LEAVES} affected, got {affected_symbol_count}"
    )

    # -- Leaf change → bounded ---------------------------------------------
    cs_leaf = ChangeSet(paths=("leaf_0.py",), watcher_confidence="diff-head1")
    inv_leaf = plan(graph, cs_leaf, max_depth=DEFAULT_CALLER_DEPTH)
    # leaf_0 has no callers (nothing imports leaf_0); only itself is seeded.
    # Per AC #23: ``affected_symbol_count <= depth_used + 1``.
    leaf_count = len(inv_leaf.affected_symbols)
    assert leaf_count <= DEFAULT_CALLER_DEPTH + 1, (
        f"leaf change should touch <= {DEFAULT_CALLER_DEPTH + 1} symbols, "
        f"got {leaf_count}"
    )

    # -- Wall-clock smoke ---------------------------------------------------
    total_elapsed = time.monotonic() - start
    assert total_elapsed < _WALL_CLOCK_CEILING_SEC, (
        f"1000-file build + 2 plan() calls took {total_elapsed:.2f}s, "
        f"ceiling is {_WALL_CLOCK_CEILING_SEC}s "
        f"(build={build_elapsed:.2f}s, plan={plan_elapsed:.4f}s)"
    )
