"""Architectural guard: ``autofix.workflow`` is a standalone subsystem.

The workflow subsystem (state-machine + verify primitives) must not
reach into ``autofix.cli``. Its public surface is two things: a
state-machine that persists transitions, and a ``verify_run`` pass
that re-scans the working tree after a fix.

Until PROACTIVE-01, ``autofix/workflow/verify.py`` did
``from autofix.cli.scan_command import _run_scan_core``. That edge
made the workflow subsystem transitively depend on the entire CLI
argparse / SARIF / event-emission stack. The fix moves the scan
core into ``autofix.scan_core`` (a non-cli module) so workflow can
call it without crossing the cli boundary.

Allowlist for top-level autofix imports under ``autofix/workflow/``:

- ``autofix.workflow``  — intra-subsystem
- ``autofix.scan_core`` — the extracted pipeline core

Function-body imports are exempt (the same convention as the
crawler / analyzer isolation tests).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_ALLOWED_PREFIXES: tuple[str, ...] = (
    "autofix.workflow",
    "autofix.scan_core",
)


def _workflow_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "autofix" / "workflow"


def _workflow_py_files() -> list[Path]:
    """Every .py file under autofix/workflow/ (recursive)."""
    return sorted(p for p in _workflow_dir().rglob("*.py") if p.is_file())


def _toplevel_imports(path: Path) -> list[str]:
    """Return the dotted module names imported at the file's top level.

    Recurses into ``if`` / ``try`` / ``else`` blocks (still module-load
    time) but NOT into function / method / class bodies (runtime
    imports are an acceptable escape hatch for circular-dep dodges).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    names.append(alias.name)
            elif isinstance(child, ast.ImportFrom) and child.module:
                names.append(child.module)
            else:
                _walk(child)

    _walk(tree)
    return names


@pytest.mark.parametrize("py_file", _workflow_py_files(), ids=lambda p: p.name)
def test_workflow_module_does_not_reach_outside_subsystem(py_file: Path) -> None:
    """Every top-level autofix import in ``autofix/workflow/**/*.py``
    must be in the allowlist. ``autofix.cli``, ``autofix.crawl``,
    ``autofix.funnel``, ``autofix.invalidation``, etc. are forbidden.
    """
    leaks: list[str] = []
    for mod in _toplevel_imports(py_file):
        if not mod.startswith("autofix."):
            continue  # stdlib / third-party — fine
        if any(mod.startswith(prefix) for prefix in _ALLOWED_PREFIXES):
            continue
        leaks.append(mod)

    assert not leaks, (
        f"{py_file.relative_to(Path(__file__).resolve().parents[3])} "
        f"imports outside the workflow subsystem at module load: {leaks!r}. "
        f"Workflow's allowed autofix imports are {_ALLOWED_PREFIXES}. "
        f"Move the import to a function body, or extract the dependency "
        f"into autofix.scan_core or another neutral module."
    )
