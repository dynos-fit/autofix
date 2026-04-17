"""Per-file symbol table builder.

Walks a :class:`autofix_next.parsing.tree_sitter.ParseResult` once and
returns a :class:`SymbolTable` with:

* ``imports`` — every top-level ``import X`` / ``from X import Y`` record,
  including bound name (what actually lands in the local namespace), raw
  source text, and 1-indexed line range.
* ``references`` — every ``identifier`` occurrence in the file that is
  *not* inside an import statement. This is the evidence the cheap
  analyzer uses to decide whether an import is used.
* ``all_exports`` — the module-level ``__all__`` literal list when it is
  a simple ``__all__ = ["a", "b"]`` assignment. Anything more complex is
  deliberately not parsed; the cheap analyzer treats a missing
  ``all_exports`` as "no re-exports known".

The walk is a single depth-first traversal over the tree-sitter tree.
``string`` and ``comment`` subtrees are entered but their children are
skipped — there is no ``identifier`` inside a string that should count
as a reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from autofix_next.parsing.tree_sitter import ParseResult


@dataclass(slots=True)
class ImportRecord:
    """One import statement's worth of information.

    ``bound_name`` is the single identifier that the import actually
    binds in the local namespace — this is the key the unused-import
    analyzer looks up in ``SymbolTable.references``:

    * ``import os``                 → ``bound_name = "os"``
    * ``import os.path``            → ``bound_name = "os"`` (leftmost segment)
    * ``import os.path as p``       → ``bound_name = "p"`` (alias wins)
    * ``from x import y``           → ``bound_name = "y"``
    * ``from x import y as z``      → ``bound_name = "z"`` (alias wins)

    Line numbers are 1-indexed and ``end_line`` is inclusive so the pair
    slots directly into ``CandidateFinding.start_line`` / ``end_line``.
    """

    bound_name: str
    raw_text: str
    start_line: int
    end_line: int


@dataclass(slots=True)
class SymbolTable:
    """Condensed evidence for the cheap per-file analyzers."""

    imports: list[ImportRecord] = field(default_factory=list)
    references: set[str] = field(default_factory=set)
    all_exports: list[str] | None = None


def _node_text(node: Any, source: bytes) -> str:
    """Return the UTF-8 text of ``node`` decoded from the source bytes."""

    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _bound_name_from_dotted(dotted_text: str) -> str:
    """Given ``os.path.foo`` return ``os`` — the name that ends up bound."""

    return dotted_text.split(".", 1)[0]


def _extract_all_exports(assignment_node: Any, source: bytes) -> list[str] | None:
    """Parse ``__all__ = [...]`` when the RHS is a plain string list.

    Returns ``None`` for any shape we do not handle (tuple-form, list
    concatenation, variable references). The cheap analyzer treats
    ``None`` as "no re-exports", which is the conservative default.
    """

    rhs = assignment_node.child_by_field_name("right")
    if rhs is None or rhs.type != "list":
        return None

    exports: list[str] = []
    for child in rhs.children:
        if child.type != "string":
            continue
        # ``string`` wraps ``string_start``, one-or-more ``string_content``,
        # ``string_end``. Concatenate every ``string_content`` child.
        parts: list[str] = []
        for sc in child.children:
            if sc.type == "string_content":
                parts.append(_node_text(sc, source))
        if parts:
            exports.append("".join(parts))
    return exports


def _process_import_statement(node: Any, source: bytes) -> list[ImportRecord]:
    """Extract one or more records from an ``import_statement`` node.

    ``import a, b as c`` produces two records; tree-sitter represents
    each name as either a bare ``dotted_name`` or an ``aliased_import``
    child at the top level of the statement.
    """

    records: list[ImportRecord] = []
    raw_text = _node_text(node, source)
    start_line = node.start_point[0] + 1
    end_line = node.end_point[0] + 1

    for child in node.children:
        if child.type == "dotted_name":
            name = _node_text(child, source)
            records.append(
                ImportRecord(
                    bound_name=_bound_name_from_dotted(name),
                    raw_text=raw_text,
                    start_line=start_line,
                    end_line=end_line,
                )
            )
        elif child.type == "aliased_import":
            name_node = child.child_by_field_name("name")
            alias_node = child.child_by_field_name("alias")
            if name_node is None or alias_node is None:
                # Malformed aliased_import; skip rather than raise so one
                # ill-formed import can't kill the whole analyzer pass.
                continue
            alias = _node_text(alias_node, source)
            records.append(
                ImportRecord(
                    bound_name=alias,
                    raw_text=raw_text,
                    start_line=start_line,
                    end_line=end_line,
                )
            )
    return records


def _process_import_from_statement(node: Any, source: bytes) -> list[ImportRecord]:
    """Extract records from ``from X import Y[, Z as W]`` statements.

    Wildcard imports (``from x import *``) are intentionally not recorded
    as :class:`ImportRecord` because they bind unknown names; the cheap
    analyzer has no way to decide whether any of them are unused. They
    are simply omitted.
    """

    records: list[ImportRecord] = []
    raw_text = _node_text(node, source)
    start_line = node.start_point[0] + 1
    end_line = node.end_point[0] + 1

    # The imported-symbol children can appear either as ``dotted_name``
    # field="name" nodes or as ``aliased_import`` nodes. Iterate raw
    # children and collect the ones that represent imported symbols after
    # the ``import`` keyword, skipping punctuation and any
    # ``wildcard_import`` entry.
    seen_import_kw = False
    for child in node.children:
        if child.type == "import":
            seen_import_kw = True
            continue
        if not seen_import_kw:
            # Everything before the ``import`` keyword is module-side
            # metadata (``from``, relative_import, module_name).
            continue
        if child.type == "wildcard_import":
            # Documented limitation: cannot reason about ``*`` re-exports.
            continue
        if child.type == "dotted_name":
            name = _node_text(child, source)
            records.append(
                ImportRecord(
                    bound_name=name,
                    raw_text=raw_text,
                    start_line=start_line,
                    end_line=end_line,
                )
            )
        elif child.type == "aliased_import":
            name_node = child.child_by_field_name("name")
            alias_node = child.child_by_field_name("alias")
            if name_node is None or alias_node is None:
                continue
            alias = _node_text(alias_node, source)
            records.append(
                ImportRecord(
                    bound_name=alias,
                    raw_text=raw_text,
                    start_line=start_line,
                    end_line=end_line,
                )
            )
    return records


def _is_module_level_all_assignment(node: Any, source: bytes) -> bool:
    """True when ``node`` is ``__all__ = <list>`` at the module top level.

    Only accepts a *direct* child-of-module expression_statement wrapping
    an assignment whose LHS is the bare identifier ``__all__``. Nested
    assignments (inside functions, classes, ``if``-blocks) are ignored —
    the conservative-by-design stance of the cheap analyzer.
    """

    if node.type != "expression_statement":
        return False
    # module -> expression_statement: parent must be module.
    if node.parent is None or node.parent.type != "module":
        return False
    if len(node.children) == 0:
        return False
    inner = node.children[0]
    if inner.type != "assignment":
        return False
    lhs = inner.child_by_field_name("left")
    if lhs is None or lhs.type != "identifier":
        return False
    return _node_text(lhs, source) == "__all__"


def build_symbol_table(parse_result: ParseResult) -> SymbolTable:
    """Produce a :class:`SymbolTable` from a parsed file.

    The walk is a single depth-first pass. Import statements are handed
    to dedicated extractors that do not re-enter the identifier walk —
    so ``import os`` does not produce a phantom reference to ``os``.
    """

    table = SymbolTable()
    source = parse_result.source_bytes
    root = parse_result.tree.root_node

    def walk(node: Any) -> None:
        t = node.type
        if t == "import_statement":
            table.imports.extend(_process_import_statement(node, source))
            return
        if t == "import_from_statement":
            table.imports.extend(_process_import_from_statement(node, source))
            return

        # Module-level __all__ assignment.
        if t == "expression_statement" and _is_module_level_all_assignment(
            node, source
        ):
            assignment = node.children[0]
            exports = _extract_all_exports(assignment, source)
            if exports is not None:
                table.all_exports = exports
            # Still descend for identifier collection — __all__ itself is
            # an identifier reference but not interesting; the strings in
            # the list literal are not identifiers.

        # Skip string / comment subtrees entirely.
        if t in ("string", "comment", "concatenated_string", "f_string"):
            return

        if t == "identifier":
            table.references.add(_node_text(node, source))

        for child in node.children:
            walk(child)

    walk(root)
    return table


__all__ = [
    "ImportRecord",
    "SymbolTable",
    "build_symbol_table",
]
