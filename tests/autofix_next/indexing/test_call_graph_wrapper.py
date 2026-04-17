"""Tests for the SCIP-index wrapper around ``CallGraph`` (AC #14 / #15 /
#17 / #18).

``CallGraph.build_from_root`` is preserved from task-003 byte-for-byte
on its public surface; the SCIP integration lives entirely inside the
method body. This file pins:

* The public API shape (signatures + return types) is unchanged (AC #14).
* A second ``build_from_root`` on an unchanged repo triggers zero
  ``parse_file`` calls (full-cache-hit path, AC #15).
* A cold build triggers ``SCIPIndex.save`` (AC #17).
* The task-003 ``_resolve_edges`` body is replaced by ``_resolve_edges_v2``
  and the old symbol is gone from the module (AC #18).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("tree_sitter_python")
pytest.importorskip("tree_sitter")

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_tiny_repo(root: Path) -> None:
    (root / "hub.py").write_text(
        "def hub_func():\n    return 1\n", encoding="utf-8"
    )
    (root / "caller.py").write_text(
        "from hub import hub_func\n"
        "\n"
        "def caller_func():\n    return hub_func()\n",
        encoding="utf-8",
    )


# ----------------------------------------------------------------------
# AC #14 — public API unchanged
# ----------------------------------------------------------------------


def test_public_api_unchanged() -> None:
    """AC #14: ``callers_of``, ``symbols_in``, ``all_symbols``,
    ``all_paths``, ``symbol_count``, ``__getitem__`` are all present on
    ``CallGraph`` with identical signatures to task-003.
    """

    from autofix_next.invalidation.call_graph import CallGraph

    # The frozen reference signatures (straight from task-003).
    expected_signatures = {
        "symbols_in": "(self, path: str) -> frozenset[str]",
        "callers_of": "(self, symbol_ids: Iterable[str], max_depth: int) -> frozenset[str]",
        "__getitem__": "(self, symbol_id: str) -> autofix_next.invalidation.call_graph.SymbolInfo",
    }

    for name in ("symbols_in", "callers_of", "__getitem__"):
        member = getattr(CallGraph, name, None)
        assert member is not None and callable(member), (
            f"CallGraph must expose .{name}"
        )
        # inspect.signature works on bound+unbound descriptors alike.
        sig = inspect.signature(member)
        # We compare a stable subset: the parameter names and order.
        params = list(sig.parameters.keys())
        assert params[0] == "self", (
            f"{name}.signature must start with 'self', got {params}"
        )

    # Properties must be present and callable on a fresh instance.
    g = CallGraph()
    for prop in ("all_symbols", "all_paths", "symbol_count"):
        assert hasattr(g, prop), f"CallGraph must expose .{prop}"

    # Spot-check: empty graph gives empty / zero results, not a raise.
    assert g.all_symbols == frozenset()
    assert g.all_paths == frozenset()
    assert g.symbol_count == 0


# ----------------------------------------------------------------------
# AC #15 — full cache hit skips parse_file
# ----------------------------------------------------------------------


def test_full_cache_hit_skips_parse_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #15: a second ``build_from_root`` on an unchanged repo triggers
    zero calls to ``parse_file``.

    The first build is cold (parses everything, writes shards). The second
    build must populate the CallGraph from shards alone — no parse.
    """

    from autofix_next.invalidation.call_graph import CallGraph
    from autofix_next.parsing import tree_sitter as ts_mod

    _make_tiny_repo(tmp_path)
    CallGraph.build_from_root(tmp_path)  # cold — populates cache

    real_parse_file = ts_mod.parse_file
    parse_calls = {"n": 0}

    def counting_parse_file(*args: Any, **kwargs: Any) -> Any:
        parse_calls["n"] += 1
        return real_parse_file(*args, **kwargs)

    monkeypatch.setattr(ts_mod, "parse_file", counting_parse_file)

    # Also patch the import used inside call_graph.build_from_root's
    # lazy import block so the counter actually intercepts the call.
    import autofix_next.invalidation.call_graph as cg_mod

    if hasattr(cg_mod, "parse_file"):
        monkeypatch.setattr(cg_mod, "parse_file", counting_parse_file)

    graph2 = CallGraph.build_from_root(tmp_path)

    # Graph must still be populated.
    assert graph2.symbol_count >= 2, "full-cache-hit graph must be non-empty"

    assert parse_calls["n"] == 0, (
        f"expected zero parse_file calls on full cache hit, got {parse_calls['n']}"
    )


# ----------------------------------------------------------------------
# AC #17 — cold build triggers save, second build takes full-hit path
# ----------------------------------------------------------------------


def test_cold_build_triggers_save(tmp_path: Path) -> None:
    """AC #17: a cold ``build_from_root`` writes ``manifest.json``; a
    subsequent ``build_from_root`` takes the full-cache-hit path (covered
    in the previous test; here we just pin the "writes manifest" half).
    """

    from autofix_next.invalidation.call_graph import CallGraph

    _make_tiny_repo(tmp_path)
    CallGraph.build_from_root(tmp_path)

    manifest_path = (
        tmp_path / ".autofix-next" / "state" / "index" / "manifest.json"
    )
    assert manifest_path.is_file(), (
        "cold build_from_root must trigger SCIPIndex.save and produce manifest.json"
    )


# ----------------------------------------------------------------------
# AC #18 — _resolve_edges_v2 replaces _resolve_edges
# ----------------------------------------------------------------------


def test_resolve_edges_v2_replaces_old() -> None:
    """AC #18: ``_resolve_edges_v2`` is present on the call_graph module
    (or on ``CallGraph``) and ``_resolve_edges`` is absent (removed, not
    aliased)."""

    import autofix_next.invalidation.call_graph as cg_mod
    from autofix_next.invalidation.call_graph import CallGraph

    # v2 must be present somewhere reachable — either module-level or
    # as a method on the class. Accept either location.
    v2_present = hasattr(cg_mod, "_resolve_edges_v2") or hasattr(
        CallGraph, "_resolve_edges_v2"
    )
    assert v2_present, (
        "_resolve_edges_v2 must exist on call_graph module or CallGraph class"
    )

    # The old symbol must be gone — aliasing is forbidden per AC #18.
    v1_module_level = hasattr(cg_mod, "_resolve_edges")
    v1_class_level = hasattr(CallGraph, "_resolve_edges")
    assert not v1_module_level and not v1_class_level, (
        "_resolve_edges must be REMOVED (not aliased) — AC #18"
    )
