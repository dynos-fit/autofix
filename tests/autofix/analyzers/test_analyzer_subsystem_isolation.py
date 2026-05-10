"""Architectural guard: ``autofix.analyzers`` is a standalone subsystem.

The analyzer subsystem must not reach into arbitrary other ``autofix.*``
subsystems. Its permitted cross-subsystem imports are:

  - ``autofix.analyzers`` — within-subsystem (always fine)
  - ``autofix.parsing``   — parse results are inputs to every analyzer
  - ``autofix.indexing.symbols`` — symbol table is an input to analyzers
  - ``autofix.evidence``  — CandidateFinding lives here
  - ``autofix.llm``       — LLM judgment analyzers call the LLM client
  - ``autofix.telemetry`` — correlation context vars

Everything else (``autofix.cli``, ``autofix.crawl``, ``autofix.funnel``,
``autofix.invalidation``, ``autofix.repair``, etc.) is OFF LIMITS.

This test walks every ``.py`` file under ``autofix/analyzers/`` and all
of its subdirectories, parses top-level imports, and rejects any import
that is not in the allowed set.

The walker SKIPS imports inside ``FunctionDef``, ``AsyncFunctionDef``, and
``ClassDef`` bodies — those are runtime imports (acceptable escape hatch for
lazy loading or circular deps). Only module-load-time imports are checked.

Parametrized by file so pytest reports the exact violating file.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Dotted prefixes that the analyzer subsystem is ALLOWED to import from.
_ALLOWED_AUTOFIX_PREFIXES: tuple[str, ...] = (
    "autofix.analyzers",
    "autofix.parsing",
    "autofix.indexing.symbols",
    "autofix.evidence",
    "autofix.llm",
    "autofix.telemetry",
)


def _analyzers_dir() -> Path:
    """Resolve autofix/analyzers/ relative to the test file."""
    return Path(__file__).resolve().parents[3] / "autofix" / "analyzers"


def _analyzer_py_files() -> list[Path]:
    """Every .py file under autofix/analyzers/ (recursive, including subdirs)."""
    analyzers = _analyzers_dir()
    return sorted(p for p in analyzers.rglob("*.py") if p.is_file())


def _toplevel_imports(path: Path) -> list[str]:
    """Return the dotted module names imported at the file's top level.

    Recurses into ``if`` / ``try`` / ``else`` / ``with`` blocks — these are
    still "module-load-time" code paths. Does NOT recurse into ``FunctionDef``,
    ``AsyncFunctionDef``, or ``ClassDef`` bodies (those are runtime imports —
    an acceptable escape hatch for unavoidable circular deps and lazy loading).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue  # don't recurse into function/method bodies
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


@pytest.mark.parametrize("py_file", _analyzer_py_files(), ids=lambda p: p.relative_to(_analyzers_dir()).as_posix())
def test_analyzer_module_does_not_reach_outside_subsystem(py_file: Path) -> None:
    """Every top-level import in ``autofix/analyzers/**/*.py`` must be either
    stdlib, third-party, or a permitted ``autofix.*`` prefix.

    Allowed ``autofix.*`` prefixes:
      - ``autofix.analyzers`` (within-subsystem)
      - ``autofix.parsing``
      - ``autofix.indexing.symbols``
      - ``autofix.evidence``
      - ``autofix.llm``
      - ``autofix.telemetry``

    Anything else under ``autofix.*`` (cli, crawl, funnel, invalidation,
    repair, ranking, dedup, …) is a contract violation.
    """
    leaks: list[str] = []
    for mod in _toplevel_imports(py_file):
        if not mod.startswith("autofix."):
            continue  # stdlib or third-party — fine
        if any(mod.startswith(prefix) for prefix in _ALLOWED_AUTOFIX_PREFIXES):
            continue  # within allowed subsystem set — fine
        leaks.append(mod)

    assert not leaks, (
        f"{py_file.relative_to(_analyzers_dir())} imports outside the permitted "
        f"analyzer subsystem set at module load: {leaks!r}. "
        f"Allowed autofix.* prefixes: {_ALLOWED_AUTOFIX_PREFIXES!r}. "
        f"Move the import to a consumer boundary or defer it to a function body."
    )
