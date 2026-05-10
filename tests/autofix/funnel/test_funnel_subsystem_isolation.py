"""Architectural guard: ``autofix.funnel`` is a standalone subsystem.

The funnel subsystem (pipeline, scheduler-orchestration, per-file dispatch)
must not reach into private internals of other ``autofix.*`` subsystems at
module-load time. Its permitted cross-subsystem imports are:

  - ``autofix.funnel``      — within-subsystem (always fine)
  - ``autofix.analyzers``   — BUT NOT ``autofix.analyzers._registry``
  - ``autofix.dedup``       — dedup cascade
  - ``autofix.evidence``    — CandidateFinding / evidence builder
  - ``autofix.events``      — event emission
  - ``autofix.indexing``    — symbol table / indexing
  - ``autofix.invalidation``— cache invalidation
  - ``autofix.languages``   — language detection
  - ``autofix.llm``         — LLM client / scheduler
  - ``autofix.migration``   — migration helpers
  - ``autofix.parsing``     — parse results
  - ``autofix.ranking``     — finding ranking
  - ``autofix.telemetry``   — correlation context vars

Explicitly forbidden (even though some start with an allowed prefix):
  - ``autofix.analyzers._registry`` — private internal module
  - ``autofix.cli``                 — CLI layer (direction reversal)
  - ``autofix.crawl``               — crawl subsystem
  - ``autofix.workflow``            — workflow subsystem

The test applies a **deny-list pre-check** before the allow-list check so
that ``autofix.analyzers._registry`` cannot slip through via the
``autofix.analyzers`` allowed prefix.

Walker skips ``FunctionDef``, ``AsyncFunctionDef``, and ``ClassDef`` bodies —
deferred function-body imports are an accepted escape hatch for lazy loading
or unavoidable circular deps. Only module-load-time imports are checked.

Parametrized per-file so pytest reports the exact violating file.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Dotted prefixes that the funnel subsystem is ALLOWED to import from.
_ALLOWED_AUTOFIX_PREFIXES: tuple[str, ...] = (
    "autofix.funnel",
    "autofix.analyzers",
    "autofix.dedup",
    "autofix.evidence",
    "autofix.events",
    "autofix.indexing",
    "autofix.invalidation",
    "autofix.languages",
    "autofix.llm",
    "autofix.migration",
    "autofix.parsing",
    "autofix.ranking",
    "autofix.telemetry",
)

# Dotted prefixes that are FORBIDDEN even if they appear to start with an
# allowed prefix. The deny-list is checked BEFORE the allow-list.
_FORBIDDEN_AUTOFIX_PREFIXES: tuple[str, ...] = (
    "autofix.analyzers._registry",
    "autofix.cli",
    "autofix.crawl",
    "autofix.workflow",
)


def _funnel_dir() -> Path:
    """Resolve autofix/funnel/ relative to the test file."""
    return Path(__file__).resolve().parents[3] / "autofix" / "funnel"


def _funnel_py_files() -> list[Path]:
    """Every .py file under autofix/funnel/ (recursive, including subdirs)."""
    funnel = _funnel_dir()
    return sorted(p for p in funnel.rglob("*.py") if p.is_file())


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
                continue  # don't recurse into function/method/class bodies
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


@pytest.mark.parametrize(
    "py_file",
    _funnel_py_files(),
    ids=lambda p: p.relative_to(_funnel_dir()).as_posix(),
)
def test_funnel_module_does_not_reach_outside_subsystem(py_file: Path) -> None:
    """Every top-level import in ``autofix/funnel/**/*.py`` must be either
    stdlib, third-party, or a permitted ``autofix.*`` prefix.

    The deny-list pre-check fires BEFORE the allow-list so that
    ``autofix.analyzers._registry`` cannot pass via the ``autofix.analyzers``
    allowed prefix. Forbidden prefixes:
      - ``autofix.analyzers._registry`` (private internal)
      - ``autofix.cli``
      - ``autofix.crawl``
      - ``autofix.workflow``

    Allowed ``autofix.*`` prefixes (after the deny-list is cleared):
      - ``autofix.funnel`` (within-subsystem)
      - ``autofix.analyzers`` (public surface only)
      - ``autofix.dedup``
      - ``autofix.evidence``
      - ``autofix.events``
      - ``autofix.indexing``
      - ``autofix.invalidation``
      - ``autofix.languages``
      - ``autofix.llm``
      - ``autofix.migration``
      - ``autofix.parsing``
      - ``autofix.ranking``
      - ``autofix.telemetry``

    Anything else under ``autofix.*`` at module-load time is a contract
    violation.
    """
    leaks: list[str] = []
    for mod in _toplevel_imports(py_file):
        if not mod.startswith("autofix."):
            continue  # stdlib or third-party — fine

        # Deny-list pre-check: reject any module that starts with a forbidden
        # prefix BEFORE checking the allow-list. This prevents
        # ``autofix.analyzers._registry`` from passing via the
        # ``autofix.analyzers`` allow-list entry.
        if any(mod.startswith(forbidden) for forbidden in _FORBIDDEN_AUTOFIX_PREFIXES):
            leaks.append(mod)
            continue

        if any(mod.startswith(prefix) for prefix in _ALLOWED_AUTOFIX_PREFIXES):
            continue  # within allowed subsystem set — fine

        leaks.append(mod)

    assert not leaks, (
        f"{py_file.relative_to(_funnel_dir())} imports outside the permitted "
        f"funnel subsystem set at module load: {leaks!r}. "
        f"Forbidden prefixes (checked first): {_FORBIDDEN_AUTOFIX_PREFIXES!r}. "
        f"Allowed autofix.* prefixes: {_ALLOWED_AUTOFIX_PREFIXES!r}. "
        f"Move the import to the public package boundary or defer it to a "
        f"function body."
    )
