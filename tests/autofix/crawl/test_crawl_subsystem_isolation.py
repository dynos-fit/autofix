"""Architectural guard: ``autofix.crawl`` is a standalone subsystem.

The crawler must not reach into any other ``autofix.*`` subsystem.
Its ONLY dependencies should be the Python stdlib + (one runtime
dep, ``pathspec``, used by ``autofixignore``). That's the contract
that makes the crawler liftable into another project.

This test walks every ``.py`` file under ``autofix/crawl/`` and
parses its top-level imports. Any ``from autofix.X import ...``
or ``import autofix.X`` where ``X != crawl`` is a contract
violation.

Bypasses for legitimate cross-subsystem cases:

* The path-level ``CallGraphAdapter`` wrapper imports
  ``autofix.invalidation.call_graph`` — but that import lives
  INSIDE ``_build_call_graph``'s function body in
  ``autofix/cli/cycle_runner.py``, NOT in the crawl subsystem.
  ``autofix/crawl/_call_graph_adapter.py`` only references
  the wrapped object structurally — no top-level
  ``autofix.invalidation`` import is required.

If you need to add such a cross-subsystem dependency: redesign so
the dependency lives at the consumer (``cli/cycle_runner.py``)
boundary, not inside the crawler. The crawler's value is
plug-and-play; cross-subsystem imports kill that.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _crawl_dir() -> Path:
    """Resolve autofix/crawl/ relative to the test file."""
    return Path(__file__).resolve().parents[3] / "autofix" / "crawl"


def _crawl_py_files() -> list[Path]:
    """Every .py file directly under autofix/crawl/ (no subdirs)."""
    crawl = _crawl_dir()
    return sorted(p for p in crawl.glob("*.py") if p.is_file())


def _toplevel_imports(path: Path) -> list[str]:
    """Return the dotted module names imported at the file's top level.

    Recurses into ``if`` / ``try`` / ``else`` blocks (these are still
    "module-load-time" code paths). Does NOT recurse into function or
    method bodies (those are runtime imports — an acceptable escape
    hatch for unavoidable circular deps and lazy heavy-loaders).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue  # don't recurse into function bodies
            if isinstance(child, ast.Import):
                for alias in child.names:
                    names.append(alias.name)
            elif isinstance(child, ast.ImportFrom) and child.module:
                names.append(child.module)
            else:
                _walk(child)

    _walk(tree)
    return names


# ---------------------------------------------------------------------------
# The architectural guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("py_file", _crawl_py_files(), ids=lambda p: p.name)
def test_crawl_module_does_not_reach_outside_subsystem(py_file: Path) -> None:
    """Every top-level import in ``autofix/crawl/*.py`` must be either
    stdlib, third-party, or another module under ``autofix.crawl.``
    The crawler is a standalone subsystem and must not depend on
    ``autofix.cli``, ``autofix.funnel``, ``autofix.repair``,
    ``autofix.parsing``, ``autofix.indexing``, ``autofix.invalidation``,
    or any other sibling.
    """
    leaks: list[str] = []
    for mod in _toplevel_imports(py_file):
        if not mod.startswith("autofix."):
            continue  # stdlib or third-party — fine
        if mod.startswith("autofix.crawl"):
            continue  # within the subsystem — fine
        leaks.append(mod)

    assert not leaks, (
        f"{py_file.name} imports outside the crawl subsystem at module load: "
        f"{leaks!r}. The crawler must be standalone — move the import to "
        f"a consumer (e.g. autofix/cli/cycle_runner.py) or defer it to a "
        f"function body."
    )
