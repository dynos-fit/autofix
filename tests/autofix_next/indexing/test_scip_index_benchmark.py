"""Hard CI perf gates for SCIP-index build (AC #20).

Two budgets, both HARD-FAIL (not skip) when exceeded:

* ``test_cold_build_under_120s`` — a cold ``CallGraph.build_from_root`` on
  the 1000-file synthetic_50k_loc_repo fixture must finish in under 120 s.
* ``test_incremental_update_under_3s`` — after mutating a single file,
  a second ``build_from_root`` must finish in under 3 s.

Wall-clock uses ``time.monotonic`` (not ``time.time``) so system clock
skew during the measurement window can't mask a regression. A
``pytest.fail`` is preferred over a bare ``assert`` so the failure
message names the observed elapsed + the budget explicitly.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_python")
pytest.importorskip("tree_sitter")

REPO_ROOT = Path(__file__).resolve().parents[3]

_COLD_BUILD_BUDGET_SECONDS = 120.0
_INCREMENTAL_UPDATE_BUDGET_SECONDS = 3.0


def test_cold_build_under_120s(synthetic_50k_loc_repo: Path) -> None:
    """AC #20: cold ``CallGraph.build_from_root`` on synth 50k LOC repo
    finishes in under 120 s wall-clock.
    """

    from autofix_next.invalidation.call_graph import CallGraph

    # Make sure no prior index exists — this is the cold-build path.
    idx_dir = synthetic_50k_loc_repo / ".autofix-next" / "state" / "index"
    if idx_dir.exists():
        import shutil

        shutil.rmtree(idx_dir)

    start = time.monotonic()
    graph = CallGraph.build_from_root(synthetic_50k_loc_repo)
    elapsed = time.monotonic() - start

    # Sanity: the graph must actually be populated — a zero-symbol build
    # would pass the time budget trivially while meaning nothing.
    assert graph.symbol_count > 0, (
        "cold build produced an empty graph; benchmark is meaningless"
    )

    # AC #17 contributor: a cold build must persist the SCIP index.
    # Without this assertion, the benchmark would pass trivially on a
    # task-003-only build that never touches the cache — which would
    # defeat the purpose of a perf gate on the SCIP-integrated path.
    manifest_path = (
        synthetic_50k_loc_repo / ".autofix-next" / "state" / "index" / "manifest.json"
    )
    assert manifest_path.is_file(), (
        "cold build_from_root must trigger SCIPIndex.save and write "
        f"manifest.json to {manifest_path}"
    )

    if elapsed >= _COLD_BUILD_BUDGET_SECONDS:
        pytest.fail(
            f"cold build took {elapsed:.2f}s, "
            f"exceeds {_COLD_BUILD_BUDGET_SECONDS}s budget (AC #20)"
        )


def test_incremental_update_under_3s(synthetic_50k_loc_repo: Path) -> None:
    """AC #20: after mutating one file, a second ``build_from_root``
    finishes in under 3 s wall-clock.

    This test depends on the cold-build path having populated the index
    already. It mutates ``leaf_0.py`` only so the incremental refresh
    has the smallest-possible scope.
    """

    from autofix_next.invalidation.call_graph import CallGraph

    # Ensure a populated index exists before we measure.
    idx_dir = synthetic_50k_loc_repo / ".autofix-next" / "state" / "index"
    if not (idx_dir / "manifest.json").is_file():
        # Prime the cache (not measured).
        CallGraph.build_from_root(synthetic_50k_loc_repo)

    # The incremental path only has meaning if the cache was actually
    # written. Without a manifest this test degenerates into a second
    # cold build — which would silently pass but verify nothing.
    assert (idx_dir / "manifest.json").is_file(), (
        "incremental benchmark requires a primed SCIP index; "
        "build_from_root did not produce manifest.json"
    )

    # Mutate exactly one file's contents.
    leaf = synthetic_50k_loc_repo / "leaf_0.py"
    original = leaf.read_text(encoding="utf-8")
    # Append a trivial line so content_hash flips but call-graph shape
    # is unchanged. The extra assignment parses cleanly.
    leaf.write_text(
        original + "\n_perf_marker = 1\n", encoding="utf-8"
    )

    try:
        start = time.monotonic()
        graph = CallGraph.build_from_root(synthetic_50k_loc_repo)
        elapsed = time.monotonic() - start

        assert graph.symbol_count > 0, (
            "incremental build produced an empty graph; benchmark is meaningless"
        )

        if elapsed >= _INCREMENTAL_UPDATE_BUDGET_SECONDS:
            pytest.fail(
                f"incremental update took {elapsed:.2f}s, "
                f"exceeds {_INCREMENTAL_UPDATE_BUDGET_SECONDS}s budget (AC #20)"
            )
    finally:
        # Restore the file so session-scoped fixture state doesn't
        # bleed into neighboring tests in the same session.
        leaf.write_text(original, encoding="utf-8")
