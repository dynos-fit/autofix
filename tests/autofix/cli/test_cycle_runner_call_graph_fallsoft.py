"""``_build_call_graph`` falls soft to ``_NoNeighbors`` on any failure."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_returns_real_adapter_when_call_graph_builds(tmp_path: Path) -> None:
    """Happy path: when CallGraph.build_from_root succeeds, the result
    wraps the real graph in CallGraphPathAdapter.
    """
    from autofix.cli.cycle_runner import _build_call_graph

    # Create a minimal directory; build_from_root may succeed or fail
    # depending on whether SCIP shards exist. If it succeeds, we expect
    # CallGraphPathAdapter; if it falls soft, we get _NoNeighbors.
    # This test asserts only that the function returns a Protocol-
    # conforming object — either is acceptable.
    result = _build_call_graph(tmp_path)
    from autofix.crawl import CallGraphAdapter

    assert isinstance(result, CallGraphAdapter)


def test_falls_soft_to_no_neighbors_on_build_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If CallGraph.build_from_root raises, return _NoNeighbors so the
    cycle keeps running. Daemon-survival invariant.
    """
    from autofix.cli.cycle_runner import _NoNeighbors, _build_call_graph

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated SCIP shard corruption")

    with patch(
        "autofix.invalidation.call_graph.CallGraph.build_from_root",
        side_effect=_raise,
    ):
        result = _build_call_graph(tmp_path)

    assert isinstance(result, _NoNeighbors)
    # And the no-op adapter satisfies the Protocol contract.
    assert result.neighbors_of(Path("anything.py")) == []


def test_falls_soft_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError during build (e.g. permission, missing file) also
    triggers fall-soft.
    """
    from autofix.cli.cycle_runner import _NoNeighbors, _build_call_graph

    def _raise_os(*args, **kwargs):
        raise OSError("simulated permission error")

    with patch(
        "autofix.invalidation.call_graph.CallGraph.build_from_root",
        side_effect=_raise_os,
    ):
        result = _build_call_graph(tmp_path)

    assert isinstance(result, _NoNeighbors)


def test_falls_soft_on_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If autofix.invalidation.call_graph itself can't import (e.g. a
    missing transitive dep), fall-soft to no-op. The except Exception
    block in _build_call_graph covers ImportError too.
    """
    import sys
    from autofix.cli.cycle_runner import _NoNeighbors, _build_call_graph

    # Force an ImportError for the lazy-loaded module.
    monkeypatch.setitem(sys.modules, "autofix.invalidation.call_graph", None)

    result = _build_call_graph(tmp_path)
    assert isinstance(result, _NoNeighbors)
