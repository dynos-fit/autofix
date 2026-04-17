"""Absolute-import path resolver for the cross-file call graph.

This module converts an :class:`~autofix_next.indexing.symbols.ImportRecord`
into a repo-relative target path (and, for ``from``-imports, the imported
symbol name). It is the bridge between the per-file symbol table and the
cross-file call graph used by the invalidation planner.

Supported shapes:

* ``from pkg.sub import name`` → ``<root>/pkg/sub/name.py`` if it exists,
  otherwise ``<root>/pkg/sub.py`` or ``<root>/pkg/sub/__init__.py`` with
  ``name`` as the imported symbol.
* ``import pkg.sub`` → ``<root>/pkg/sub.py`` or ``<root>/pkg/sub/__init__.py``
  with no symbol.
* Aliases (``import x as y``, ``from x import y as z``) — the alias binds
  the name in the caller's namespace, but the resolver still points at the
  real file and real symbol.

Documented non-goals (documented false-negatives — edges are missed, never wrong):

- Relative imports (``from . import x``, ``from ..pkg import y``) return
  ``None``. Resolving them correctly requires knowing the importing file's
  package position, which the cheap resolver deliberately ignores.
- Star / wildcard imports (``from x import *``) return ``None``; the set of
  bound names is unknown at parse time. In practice these never appear in
  an :class:`ImportRecord` because the symbol-table builder filters them.
- Dynamic imports (``importlib.import_module(...)``, ``__import__(...)``)
  return ``None`` — they are not observable at parse time.
- Third-party imports — anything whose resolved path falls outside
  ``repo_root`` or is not present in ``all_paths`` returns ``None``.
- Stdlib imports — treated identically to third-party: outside ``repo_root``
  so the resolver returns ``None``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from autofix_next.indexing.symbols import ImportRecord


@dataclass(slots=True, frozen=True)
class ResolvedImport:
    """One resolved import edge.

    ``target_path`` is a repo-relative POSIX path to a ``.py`` file that
    exists in ``all_paths``. ``target_symbol`` is the symbol name inside
    that file when the record is a ``from``-import resolved to a module
    file (rather than a submodule file); it is ``None`` for ``import x``
    statements and for ``from x import y`` where ``y`` itself is a
    submodule file.
    """

    target_path: str
    target_symbol: Optional[str]


def _module_to_candidates(repo_root: Path, module_dotted: str) -> list[Path]:
    """Return candidate file paths for a dotted module name.

    For ``"pkg.sub"`` this yields ``[repo_root/pkg/sub.py,
    repo_root/pkg/sub/__init__.py]`` in priority order. The caller is
    responsible for checking each candidate against ``all_paths``.
    """

    parts = module_dotted.split(".")
    if not parts or any(part == "" for part in parts):
        # Defensive: an empty dotted segment means a malformed module name
        # (e.g. leading dot after stripping). Bail rather than produce
        # nonsense candidates.
        return []
    base = repo_root.joinpath(*parts)
    return [base.with_suffix(".py"), base / "__init__.py"]


def _rel_posix_if_under_root(candidate: Path, repo_root: Path) -> Optional[str]:
    """Return the repo-relative POSIX form of ``candidate`` or ``None``.

    ``None`` means the candidate is not under ``repo_root`` — the caller
    should treat that as a non-goal and skip it.
    """

    try:
        rel = candidate.relative_to(repo_root)
    except ValueError:
        return None
    # ``as_posix`` normalises separators on every platform.
    return rel.as_posix()


def resolve(
    record: ImportRecord,
    *,
    repo_root: Path,
    all_paths: frozenset[str],
    source_file: Optional[str] = None,
) -> Optional[ResolvedImport]:
    """Resolve an :class:`ImportRecord` to a repo-relative target path.

    Returns ``None`` for any non-goal case enumerated in the module
    docstring: relative imports, star imports, dynamic imports, and any
    import whose resolved path is not under ``repo_root`` / not present
    in ``all_paths`` (stdlib, third-party, typos).
    """

    raw = record.raw_text.strip()
    if not raw:
        return None

    # Parse the raw import text with the stdlib AST. Any SyntaxError here
    # means the record is malformed; treat it as a non-goal rather than
    # propagating the error to the caller.
    try:
        module_ast = ast.parse(raw, mode="exec")
    except SyntaxError:
        return None

    if not module_ast.body:
        return None
    stmt = module_ast.body[0]

    if isinstance(stmt, ast.Import):
        return _resolve_import(stmt, repo_root=repo_root, all_paths=all_paths)

    if isinstance(stmt, ast.ImportFrom):
        return _resolve_import_from(
            stmt,
            record=record,
            repo_root=repo_root,
            all_paths=all_paths,
            source_file=source_file,
        )

    # Any other statement shape (expression, assignment, etc.) is not an
    # import we know how to resolve — return None per the non-goal policy.
    return None


def _resolve_import(
    stmt: ast.Import,
    *,
    repo_root: Path,
    all_paths: frozenset[str],
) -> Optional[ResolvedImport]:
    """Handle ``import pkg.sub [as alias]``.

    ``ImportRecord`` always represents a single bound name, so we take the
    first ``ast.alias`` — upstream code guarantees one record per alias.
    """

    if not stmt.names:
        return None
    name = stmt.names[0]
    module_dotted = name.name
    if not module_dotted:
        return None

    for candidate in _module_to_candidates(repo_root, module_dotted):
        rel_posix = _rel_posix_if_under_root(candidate, repo_root)
        if rel_posix is None:
            continue
        if rel_posix in all_paths:
            return ResolvedImport(target_path=rel_posix, target_symbol=None)

    return None


def _resolve_import_from(
    stmt: ast.ImportFrom,
    *,
    record: ImportRecord,
    repo_root: Path,
    all_paths: frozenset[str],
    source_file: Optional[str] = None,
) -> Optional[ResolvedImport]:
    """Handle ``from pkg.sub import name [as alias]``.

    Relative imports (``stmt.level > 0``) resolve against ``source_file``'s
    parent package when ``source_file`` is provided; otherwise they return
    None (documented non-goal fallback). Star imports are always a non-goal.
    """

    if stmt.level > 0:
        # Relative import — resolve against source_file's package context if available.
        if source_file is None:
            return None
        # source_file is a repo-relative POSIX path to a .py file. The package
        # context is the chain of parent directories. For stmt.level=1, the
        # package root is source_file's containing directory; level=2 is that
        # directory's parent; etc.
        src_parts = source_file.split("/")
        if not src_parts or not src_parts[-1].endswith(".py"):
            return None
        # Drop the .py filename; the remaining parts form the package chain.
        pkg_chain = src_parts[:-1]
        # Ascend stmt.level levels (level=1 means current package; level=2 is parent, etc.)
        if stmt.level > len(pkg_chain):
            return None
        base_parts = pkg_chain[: len(pkg_chain) - stmt.level + 1]
        # If stmt.module is set, append its dotted parts to the base.
        if stmt.module:
            base_parts = base_parts + stmt.module.split(".")
        # Build an absolute-style module name and delegate to the shared resolution logic.
        dotted = ".".join(p for p in base_parts if p)
        if not dotted:
            return None
        # Reuse the name-resolution below with a synthetic absolute module reference.
        stmt_module_effective: Optional[str] = dotted
    else:
        if stmt.module is None:
            # ``from . import x`` parses with module=None AND level=0 only if
            # malformed. Guard retained for clarity.
            return None
        stmt_module_effective = stmt.module
    if not stmt.names:
        return None

    # Pick the alias whose bound name matches the record's bound_name so
    # multi-target ``from x import a, b as c`` statements resolve to the
    # correct symbol. Fall back to the first alias when the record pre-dates
    # the alias list or the raw text only carries one name.
    chosen: Optional[ast.alias] = None
    for alias in stmt.names:
        if alias.name == "*":
            # Star import — documented non-goal.
            return None
        if (alias.asname or alias.name) == record.bound_name:
            chosen = alias
            break
    if chosen is None:
        chosen = stmt.names[0]
        if chosen.name == "*":
            return None

    imported_name = chosen.name
    module_dotted = stmt_module_effective
    if not imported_name or not module_dotted:
        return None

    # Rule 1: ``from pkg.sub import name`` → try ``<root>/pkg/sub/name.py``
    # first. If the imported symbol is itself a submodule file that wins.
    parts_as_module = module_dotted.split(".")
    if all(parts_as_module) and imported_name:
        submodule_path = repo_root.joinpath(
            *parts_as_module, imported_name
        ).with_suffix(".py")
        rel_posix = _rel_posix_if_under_root(submodule_path, repo_root)
        if rel_posix is not None and rel_posix in all_paths:
            return ResolvedImport(target_path=rel_posix, target_symbol=None)

    # Rule 2: fall back to ``<root>/pkg/sub.py`` or
    # ``<root>/pkg/sub/__init__.py`` with ``imported_name`` as the target
    # symbol within that file.
    for candidate in _module_to_candidates(repo_root, module_dotted):
        rel_posix = _rel_posix_if_under_root(candidate, repo_root)
        if rel_posix is None:
            continue
        if rel_posix in all_paths:
            return ResolvedImport(
                target_path=rel_posix, target_symbol=imported_name
            )

    return None


__all__ = [
    "ResolvedImport",
    "resolve",
]
