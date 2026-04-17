"""Integration tests for ``CallGraph.build_from_root``.

Covers:

* AC #7  — ``git ls-files`` enumeration matches ``os.walk`` fallback for
  identical file contents (same symbols collected).
* AC #8  — top-level ``function_definition``, ``class_definition`` and
  methods nested inside classes are collected; functions nested inside
  functions are NOT collected.
* AC #10 — two-pass builder writes cross-file caller→callee edges via the
  ``import_resolver`` bridge; syntax-error files are skipped without
  crashing the build.

These tests require the native tree-sitter Python grammar; a missing grammar
causes the whole module to be ``importorskip``-ed rather than ERROR-ed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Tree-sitter grammar is required for the two-pass builder. If the native
# grammar wheel is not installed, skip the entire module rather than emit
# a collection-time ERROR.
pytest.importorskip("tree_sitter_python")
pytest.importorskip("tree_sitter")

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Small fixture helpers
# ---------------------------------------------------------------------------


def _write_tree(root: Path, files: dict[str, str]) -> None:
    """Write ``files`` (relpath -> text) under ``root``, creating subdirs."""
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def _git_init_and_add(root: Path) -> None:
    """Initialize a git repo under ``root`` and ``git add`` all files.

    Kept isolated so the non-git-path test can skip this step entirely.
    """
    subprocess.run(
        ["git", "init", "-q"], cwd=str(root), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "a@b.c"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "tester"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-A"], cwd=str(root), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# AC #7 — git-ls-files vs os.walk parity
# ---------------------------------------------------------------------------


def test_build_from_root_git_and_walk_match(tmp_path: Path) -> None:
    """AC #7: identical file contents produce identical symbol sets via
    git-ls-files and via the os.walk fallback."""
    from autofix_next.invalidation.call_graph import CallGraph

    files = {
        "a.py": "def one():\n    pass\n",
        "pkg/b.py": "def two():\n    pass\n\nclass K:\n    def m(self):\n        pass\n",
        "pkg/__init__.py": "",
    }

    # Variant A: inside a git repo.
    root_a = tmp_path / "git_variant"
    root_a.mkdir()
    _write_tree(root_a, files)
    _git_init_and_add(root_a)

    # Variant B: same files, but no .git — forces os.walk fallback.
    root_b = tmp_path / "walk_variant"
    root_b.mkdir()
    _write_tree(root_b, files)

    ga = CallGraph.build_from_root(root_a)
    gb = CallGraph.build_from_root(root_b)

    # Compare the set of symbol *names* (paths differ between the two roots,
    # but repo-relative paths should match since we wrote identical trees).
    assert ga.all_symbols == gb.all_symbols
    assert ga.all_paths == gb.all_paths


# ---------------------------------------------------------------------------
# AC #8 — what kinds of definitions contribute symbols
# ---------------------------------------------------------------------------


def test_collects_top_level_functions_classes_and_methods(tmp_path: Path) -> None:
    """AC #8: top-level functions + classes + methods-in-classes become
    symbols; functions nested inside other functions do NOT."""
    from autofix_next.invalidation.call_graph import CallGraph

    _write_tree(
        tmp_path,
        {
            "mod.py": (
                "def top_level_fn():\n"
                "    def nested_inside_fn():\n"
                "        return 1\n"
                "    return nested_inside_fn\n"
                "\n"
                "class TopLevelClass:\n"
                "    def method_a(self):\n"
                "        return 1\n"
                "\n"
                "    def method_b(self):\n"
                "        return 2\n"
            ),
        },
    )

    graph = CallGraph.build_from_root(tmp_path)
    names = {graph[s].name for s in graph.all_symbols}

    assert "top_level_fn" in names
    assert "TopLevelClass" in names
    assert "method_a" in names
    assert "method_b" in names
    # AC #8 explicitly excludes functions nested inside functions.
    assert "nested_inside_fn" not in names


# ---------------------------------------------------------------------------
# AC #10 — two-pass builder wires cross-file edges
# ---------------------------------------------------------------------------


def test_two_pass_builds_cross_file_edges(tmp_path: Path) -> None:
    """AC #10: pass 1 collects all symbols; pass 2 walks references and
    writes caller→callee edges into both ``_callees[caller]`` and
    ``_callers[callee]`` via the import-resolver bridge."""
    from autofix_next.invalidation.call_graph import CallGraph

    _write_tree(
        tmp_path,
        {
            "b.py": "def b_func():\n    return 1\n",
            "a.py": (
                "from b import b_func\n"
                "\n"
                "def a_func():\n"
                "    return b_func()\n"
            ),
        },
    )

    graph = CallGraph.build_from_root(tmp_path)

    a_id = "a.py::a_func"
    b_id = "b.py::b_func"
    assert a_id in graph.all_symbols
    assert b_id in graph.all_symbols

    # Dual-direction invariant: caller→callee must appear in both maps.
    assert b_id in graph._callees.get(a_id, set()), (
        "a_func should have b_func in its _callees set"
    )
    assert a_id in graph._callers.get(b_id, set()), (
        "b_func should have a_func in its _callers set"
    )

    # callers_of(b_func) should surface a_func at depth 1.
    assert graph.callers_of([b_id], max_depth=1) == frozenset({a_id})


def test_build_skips_syntax_error_files(tmp_path: Path) -> None:
    """Implicit requirement under AC #10: a syntax-error file contributes
    zero symbols and does NOT crash the build pass."""
    from autofix_next.invalidation.call_graph import CallGraph

    _write_tree(
        tmp_path,
        {
            "good.py": "def good_fn():\n    return 1\n",
            # Deliberately malformed Python. tree-sitter will produce an ERROR
            # node; the builder must skip this file without raising.
            "bad.py": "def oops(:\n    return !!\n",
        },
    )

    graph = CallGraph.build_from_root(tmp_path)
    assert "good.py::good_fn" in graph.all_symbols
    # No symbol from bad.py should leak through.
    bad_symbols = [s for s in graph.all_symbols if s.startswith("bad.py::")]
    assert bad_symbols == [], f"unexpected symbols from syntax-error file: {bad_symbols}"
