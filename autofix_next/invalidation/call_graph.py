"""In-memory call graph for cross-file invalidation (AC #4 / #5 / #6 / #7 / #8 / #10).

Responsibilities
----------------
* Model a single symbol (function / class / class method) as an immutable
  :class:`SymbolInfo` record. The ``symbol_id`` format is
  ``"<repo-relative-path>::<qualified-name>"`` where the qualified name is
  ``name`` for top-level functions and classes, and ``"ClassName.method"``
  for methods declared inside a class body (AC #8).
* Maintain four private dicts on :class:`CallGraph` (AC #4):

    - ``_symbols``          — ``symbol_id → SymbolInfo``.
    - ``_callees``          — ``caller_id → set[callee_id]`` (downstream).
    - ``_callers``          — ``callee_id → set[caller_id]`` (upstream;
                              populated in parallel with ``_callees`` so
                              the dual-direction invariant of AC #10 holds).
    - ``_path_to_symbols``  — ``repo-relative path → set[symbol_id]`` so
                              :meth:`symbols_in` is an O(1) dict lookup.

* Expose a small, frozen-set-based public surface (AC #5): ``symbols_in``,
  ``callers_of`` (BFS upward over ``_callers``), ``__getitem__`` for
  ``SymbolInfo`` lookup, and the ``all_symbols`` / ``all_paths`` /
  ``symbol_count`` properties.

* Provide :meth:`CallGraph.build_from_root` (AC #7 / #8 / #10), a two-pass
  classmethod that:

    1. Enumerates Python files under ``root`` using ``git ls-files '*.py'``
       when available, falling back to :func:`os.walk` with a conservative
       exclude set (``.git``, ``__pycache__``, ``.venv``, etc.).
    2. **Pass 1** — parses every enumerated file with
       :func:`autofix_next.parsing.tree_sitter.parse_file` and collects
       top-level ``function_definition`` / ``class_definition`` plus
       ``function_definition`` nodes nested **one** level inside a class
       body. Functions nested inside other functions are deliberately
       excluded (AC #8). Syntax-error files are silently skipped so a
       single broken source can't kill the whole build.
    3. **Pass 2** — walks each file's symbol table
       (:func:`autofix_next.indexing.symbols.build_symbol_table`), resolves
       each import record through
       :func:`autofix_next.invalidation.import_resolver.resolve`, and wires
       caller→callee edges in both ``_callees`` and ``_callers``.

Imports of :mod:`autofix_next.invalidation.import_resolver` are deferred
to the body of :meth:`build_from_root` — this module must remain
importable even before seg-3 (the resolver) lands, so downstream callers
can already construct and poke at an empty :class:`CallGraph`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

# Directories excluded from the ``os.walk`` fallback (AC #7). The set is
# intentionally narrow: only build artefacts, VCS metadata, vendored
# virtualenvs, and the autofix output directories. Kept module-private
# because callers don't need to override it — the git-ls-files branch
# honours ``.gitignore`` on its own.
_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        "build",
        "dist",
        ".autofix",
        ".autofix-next",
        ".dynos",
    }
)


@dataclass(slots=True, frozen=True)
class SymbolInfo:
    """Immutable metadata for a single collected symbol (AC #4 / #8).

    Attributes
    ----------
    symbol_id:
        Stable cross-file identifier, ``"<relpath>::<qualified-name>"``.
        For a top-level ``def foo`` in ``pkg/mod.py`` this is
        ``"pkg/mod.py::foo"``; for ``def method`` inside ``class Klass``
        in the same file it is ``"pkg/mod.py::Klass.method"``.
    path:
        Repo-relative POSIX path of the file the symbol is declared in.
    name:
        Qualified name (``"foo"`` or ``"Klass.method"``). Matches the
        right-hand side of ``symbol_id``.
    kind:
        ``"function"`` (plain function or class method) or ``"class"``.
    start_line / end_line:
        1-indexed line range spanning the full node in the source file.
        ``end_line`` is inclusive.
    """

    symbol_id: str
    path: str
    name: str
    kind: Literal["function", "class"]
    start_line: int
    end_line: int


class CallGraph:
    """Directed call graph of Python symbols across a repo (AC #4).

    The graph is a plain in-memory structure. Callers construct it via
    :meth:`build_from_root` in production; tests may also instantiate it
    with :class:`CallGraph.__new__` and hand-populate the private dicts.
    """

    __slots__ = (
        "_symbols",
        "_callees",
        "_callers",
        "_path_to_symbols",
        "_root",
        "_repo_root",
        # ``__dict__`` is included alongside the named slots so the
        # invalidation planner's fresh-instance test (AC #14) can
        # monkey-patch :meth:`callers_of` with a sentinel that raises on
        # accidental traversal. The named slots still provide fast, typo-
        # safe access for the documented fields; ``__dict__`` only matters
        # when a caller deliberately overrides a bound method.
        "__dict__",
    )

    def __init__(self) -> None:
        # The four dicts documented in the module docstring (AC #4). Kept
        # as plain builtins so hand-built test fixtures can poke at them
        # without routing through a builder method.
        self._symbols: dict[str, SymbolInfo] = {}
        self._callees: dict[str, set[str]] = {}
        self._callers: dict[str, set[str]] = {}
        self._path_to_symbols: dict[str, set[str]] = {}
        self._root: Path | None = None
        # ``_repo_root`` is the canonical handle the invalidation planner
        # uses to locate on-disk files for new-file on-the-fly parsing
        # (AC #16) and existence checks (AC #17 / #18). ``build_from_root``
        # stamps this alongside ``_root``; tests that hand-build a graph via
        # ``CallGraph.__new__`` may also assign it directly (slot reserved
        # above).
        self._repo_root: Path | None = None

    # ------------------------------------------------------------------
    # Public read surface (AC #5)
    # ------------------------------------------------------------------

    def symbols_in(self, path: str) -> frozenset[str]:
        """Return the symbols declared in ``path`` (repo-relative).

        Returns an empty frozenset when ``path`` is not known to the graph
        — callers use this to distinguish "no symbols here" from "this
        file was never scanned" at their own discretion.
        """

        return frozenset(self._path_to_symbols.get(path, ()))

    def callers_of(
        self, symbol_ids: Iterable[str], max_depth: int
    ) -> frozenset[str]:
        """Return symbols that transitively reach ``symbol_ids`` (AC #6).

        A breadth-first walk upward over ``_callers``. Semantics pinned by
        the unit tests:

        * ``max_depth <= 0`` → empty frozenset (no expansion).
        * The seeds themselves are **never** included in the return value.
        * Cycles are visited at most once.
        * Passing a bare ``str`` raises :class:`TypeError` — a bare string
          would otherwise iterate character-by-character and silently
          produce nonsense seeds.
        """

        if isinstance(symbol_ids, str):
            # Iterable[str] includes str itself, which would silently
            # shatter into per-character "symbol ids". Reject explicitly.
            raise TypeError(
                "symbol_ids must be an iterable of str, not str itself"
            )
        if max_depth <= 0:
            return frozenset()

        seeds = set(symbol_ids)
        # ``visited`` tracks everything we've already enqueued (seeds +
        # any caller we've pushed into the frontier) so a cycle can't
        # cause us to revisit the same node.
        visited: set[str] = set(seeds)
        frontier: set[str] = set(seeds)
        result: set[str] = set()

        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for node in frontier:
                for caller in self._callers.get(node, ()):
                    if caller in visited:
                        continue
                    visited.add(caller)
                    result.add(caller)
                    next_frontier.add(caller)
            if not next_frontier:
                # Fully exhausted the reachable caller set; no point
                # spinning through the remaining depth budget.
                break
            frontier = next_frontier
        return frozenset(result)

    def __getitem__(self, symbol_id: str) -> SymbolInfo:
        """Return the :class:`SymbolInfo` for ``symbol_id``.

        Raises :class:`KeyError` on an unknown id — matches dict semantics
        so callers can use ``try/except KeyError`` or ``in`` before
        indexing.
        """

        return self._symbols[symbol_id]

    @property
    def all_symbols(self) -> frozenset[str]:
        """All symbol ids currently in the graph, as a frozenset."""

        return frozenset(self._symbols)

    @property
    def all_paths(self) -> frozenset[str]:
        """All file paths that contributed at least one symbol."""

        return frozenset(self._path_to_symbols)

    @property
    def symbol_count(self) -> int:
        """Total number of symbols — used by telemetry / invalidation events."""

        return len(self._symbols)

    # ------------------------------------------------------------------
    # Builder (AC #7 / #8 / #10)
    # ------------------------------------------------------------------

    @classmethod
    def build_from_root(cls, root: Path) -> "CallGraph":
        """Build a fully-populated :class:`CallGraph` by scanning ``root``.

        Two passes are run sequentially:

        1. Enumerate every ``*.py`` file below ``root`` (git-ls-files with
           os.walk fallback), parse it, and record every top-level function,
           class, and class method as a :class:`SymbolInfo` (AC #7 / #8).
        2. For each parsed file, build its symbol table, resolve every
           import through :func:`import_resolver.resolve`, and wire
           caller→callee edges from every local symbol in the file to
           every resolved-target symbol the file references (AC #10).

        Syntax-error files are silently skipped in pass 1 and so contribute
        zero symbols and zero edges.
        """

        graph = cls()
        graph._root = root
        graph._repo_root = root

        # Pass 0 — enumerate.
        rel_paths = _enumerate_python_files(root)

        # Pass 1 — parse + symbol collection. We hold onto the
        # ``ParseResult`` per file so pass 2 can reuse the tree and bytes
        # without re-reading / re-parsing.
        # Lazy import keeps this module importable when tree-sitter isn't
        # installed (the grammar is only required at build time).
        from autofix_next.parsing.tree_sitter import (
            TreeSitterLoadError,
            parse_file,
        )

        parse_results: dict[str, Any] = {}
        for relpath in rel_paths:
            abs_path = root / relpath
            try:
                pr = parse_file(abs_path, repo_root=root)
            except TreeSitterLoadError:
                # Grammar load/parse failure for this specific file;
                # skip it rather than aborting the whole build.
                continue
            except (FileNotFoundError, OSError):
                # File disappeared between enumeration and parse, or is
                # unreadable (permission, symlink cycle, etc.). Skip.
                continue
            # Tree-sitter is error-tolerant — a malformed source still
            # parses to a tree, but ``root_node.has_error`` is True. The
            # shape of such a tree is unreliable (partial / synthesized
            # nodes) so we skip the whole file: no symbols, no edges.
            # This satisfies the "syntax-error files contribute zero
            # symbols" contract implied by AC #10.
            if getattr(pr.tree.root_node, "has_error", False):
                continue

            parse_results[relpath] = pr

            for sym in _extract_symbols(pr, relpath):
                graph._symbols[sym.symbol_id] = sym
                graph._path_to_symbols.setdefault(relpath, set()).add(
                    sym.symbol_id
                )

        # Pass 2 — edge resolution. Factored into a method so seg-3 can
        # land before the edge-building logic is exercised, and so tests
        # can inject synthetic graphs without invoking import resolution.
        graph._resolve_edges(parse_results)

        return graph

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_edges(self, parse_results: dict[str, Any]) -> None:
        """Populate ``_callees`` / ``_callers`` from per-file references.

        For every file we build its :class:`SymbolTable`, resolve each
        import through the import resolver (lazy-imported — the resolver
        module may not exist yet in very early bootstrap environments),
        and emit a caller→callee edge from each symbol declared in the
        file to every resolved target referenced from that file.

        This is deliberately a conservative over-approximation: we don't
        attempt to match references to specific enclosing functions. The
        invalidation planner only needs an edge to exist, not to be
        pinpoint-precise about which intra-file caller owns it.
        """

        # Lazy imports — kept inside this method so the module can be
        # imported even before seg-3 (import_resolver) lands.
        try:
            from autofix_next.invalidation.import_resolver import resolve
        except ImportError:
            # Resolver not yet available; leave the graph edge-free.
            # Planner fallback handles the "no edges" case gracefully.
            return
        from autofix_next.indexing.symbols import build_symbol_table

        root = self._root
        if root is None:
            # _resolve_edges is only meaningful after build_from_root has
            # stamped ``_root``; defensive no-op for hand-built graphs.
            return

        known_paths = frozenset(self._path_to_symbols)

        for relpath, pr in parse_results.items():
            try:
                symtab = build_symbol_table(pr)
            except Exception:
                # A malformed tree-sitter tree can surface AttributeError
                # on missing fields; treat it the same as "no symbols"
                # rather than crashing the whole build.
                continue

            # Map each bound import name in this file to the concrete
            # target ``symbol_id`` the resolver points to (if any).
            import_map: dict[str, str] = {}
            for record in symtab.imports:
                try:
                    resolved = resolve(
                        record, repo_root=root, all_paths=known_paths
                    )
                except Exception:
                    # Any resolver-side failure for a single import is
                    # not fatal — we simply miss that edge.
                    continue
                if resolved is None:
                    continue
                target_symbol = getattr(resolved, "target_symbol", None)
                target_path = getattr(resolved, "target_path", None)
                if target_symbol is None or target_path is None:
                    # ``import pkg.sub`` style — no symbol-level edge
                    # (only module granularity). Planner falls back on
                    # file-level invalidation for these cases.
                    continue
                target_sid = f"{target_path}::{target_symbol}"
                if target_sid in self._symbols:
                    import_map[record.bound_name] = target_sid

            # Same-file references resolve against the file's own symbol
            # table so intra-file calls also show up as edges.
            local_sym_ids = self._path_to_symbols.get(relpath, set())
            local_map: dict[str, str] = {}
            for sid in local_sym_ids:
                info = self._symbols[sid]
                # Top-level functions / classes are keyed by their bare
                # name; methods are keyed by ``"Klass.method"`` which
                # tree-sitter never emits as a single identifier, so
                # they naturally only appear as call edges when the
                # method name itself is referenced somewhere else.
                local_map.setdefault(info.name, sid)

            # Collect the set of *resolved* targets this file references.
            referenced_targets: set[str] = set()
            for ref_name in symtab.references:
                if ref_name in import_map:
                    referenced_targets.add(import_map[ref_name])
                elif ref_name in local_map:
                    referenced_targets.add(local_map[ref_name])

            # Wire caller→callee edges. We populate BOTH directions so
            # AC #10's dual-direction invariant holds on every edge.
            for caller_sid in local_sym_ids:
                for callee_sid in referenced_targets:
                    if caller_sid == callee_sid:
                        # Self-edges add noise without changing
                        # reachability for the planner; skip them.
                        continue
                    self._callees.setdefault(caller_sid, set()).add(callee_sid)
                    self._callers.setdefault(callee_sid, set()).add(caller_sid)


# ----------------------------------------------------------------------
# File enumeration + symbol extraction
# ----------------------------------------------------------------------


def _enumerate_python_files(root: Path) -> list[str]:
    """Return the set of repo-relative ``*.py`` paths under ``root`` (AC #7).

    Uses ``git ls-files '*.py'`` when the directory is a git working tree
    (the fast, .gitignore-aware path). Falls back to :func:`os.walk` with
    :data:`_EXCLUDE_DIRS` pruned when git is unavailable, the directory is
    not a repo, or the subprocess times out.
    """

    try:
        proc = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # ``git`` not on PATH, ran too long, or some other IO failure —
        # fall through to the os.walk branch.
        proc = None

    if proc is not None and proc.returncode == 0:
        # ``git ls-files *.py`` returns paths relative to ``cwd``. Filter
        # and normalize so the result is stable across platforms.
        results = [
            line.strip()
            for line in proc.stdout.splitlines()
            if line.strip().endswith(".py")
        ]
        return sorted(results)

    # os.walk fallback — prunes the excluded directories in-place so we
    # never descend into ``.venv``, ``node_modules``, ``.git``, etc.
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            abs_path = Path(dirpath) / fn
            try:
                rel = abs_path.relative_to(root)
            except ValueError:
                # Shouldn't happen — os.walk only yields under ``root`` —
                # but skip defensively if a symlink jumps outside.
                continue
            out.append(rel.as_posix())
    return sorted(out)


def _extract_symbols(parse_result: Any, relpath: str) -> list[SymbolInfo]:
    """Collect symbols from a single parsed file (AC #8).

    Contract:

    * Every top-level ``function_definition`` and ``class_definition`` is
      a symbol.
    * Inside each class body, every *direct child* ``function_definition``
      is a symbol with the name ``"ClassName.method"``.
    * Functions nested inside other functions are **not** symbols.
    * Nodes with missing names (syntax errors mid-definition) are skipped.
    """

    tree = parse_result.tree
    source_bytes = parse_result.source_bytes
    symbols: list[SymbolInfo] = []

    def _name_of(node: Any) -> str | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        return source_bytes[name_node.start_byte : name_node.end_byte].decode(
            "utf-8", errors="replace"
        )

    def _mk(
        node: Any,
        local_name: str,
        qualified_name: str,
        kind: Literal["function", "class"],
    ) -> SymbolInfo:
        # tree-sitter row positions are 0-indexed; convert to the
        # conventional 1-indexed line numbers used everywhere else in
        # the codebase (see ImportRecord.start_line for parity).
        #
        # ``qualified_name`` goes into the ``symbol_id`` so the id is
        # globally unique inside the file (AC #8: ``Klass.method``);
        # ``local_name`` is stored in ``.name`` so callers can pretty-print
        # or match against bare identifiers that tree-sitter emits during
        # the reference walk.
        return SymbolInfo(
            symbol_id=f"{relpath}::{qualified_name}",
            path=relpath,
            name=local_name,
            kind=kind,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )

    root_node = tree.root_node
    for child in root_node.children:
        if child.type == "function_definition":
            nm = _name_of(child)
            if nm is not None:
                symbols.append(_mk(child, nm, nm, "function"))
            # Do NOT descend into the function body — AC #8 forbids
            # collecting functions nested inside functions.
        elif child.type == "class_definition":
            cls_name = _name_of(child)
            if cls_name is None:
                continue
            symbols.append(_mk(child, cls_name, cls_name, "class"))

            # Enter the class body exactly one level to pick up methods.
            body = child.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type != "function_definition":
                    continue
                method_name = _name_of(member)
                if method_name is None:
                    continue
                # Symbol id is ``Klass.method`` (AC #8); ``.name`` is the
                # bare ``method`` so the reference-walk map can match it
                # against the identifier tree-sitter emits at call sites.
                symbols.append(
                    _mk(
                        member,
                        method_name,
                        f"{cls_name}.{method_name}",
                        "function",
                    )
                )

    return symbols


__all__ = [
    "SymbolInfo",
    "CallGraph",
]
