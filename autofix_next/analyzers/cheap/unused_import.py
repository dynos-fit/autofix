"""Conservative ``unused-import.intra-file`` analyzer.

The rule asks one question per :class:`ImportRecord`: is the bound name
ever referenced in the same file, or explicitly re-exported through
``__all__``? If not, it emits one :class:`CandidateFinding`.

Documented known limitations
----------------------------
Every item below is a *documented, accepted false-positive source*, not
a bug. The rule is intentionally conservative in the other direction
(never falsely claim an import is used), because unused-import autofix
is the "safest" patch we apply and the downstream LLM verifier is the
second line of defense. False positives cost one LLM call; false
negatives cost correctness.

* ``__all__`` re-export — **handled**. A bound name listed in a
  module-level ``__all__ = [...]`` literal list is treated as used.
* ``TYPE_CHECKING`` blocks — **NOT handled**. Imports guarded by
  ``if TYPE_CHECKING:`` that are only referenced in string annotations
  will be flagged as unused. This is the single largest false-positive
  class and is documented in design-decisions.md §Non-goals.
* Side-effect imports (``import readline``, ``import pkg_resources``,
  etc.) — **NOT handled**. The rule cannot know that importing a module
  registers a codec, installs a readline hook, or patches a global. The
  LLM verifier has the prose context to reject these fixes.
* String annotations and ``typing.TYPE_CHECKING`` patterns more
  generally — **NOT handled**. Tree-sitter identifier walk does not
  enter ``string`` nodes, so ``def f(x: "os.PathLike") -> None`` does
  not count as a reference to ``os``.
* Star imports (``from x import *``) — **IGNORED**. No record is
  emitted for wildcard imports because the bound-name set is unknown.

The ``finding_id`` fingerprint is derived from the rule id, repo-relative
path, bound name, and normalized (whitespace-collapsed) import text so
that the same finding on the same line in two different runs hashes
identically, matching AC #18.
"""

from __future__ import annotations

from autofix_next.evidence.fingerprints import compute_finding_fingerprint
from autofix_next.evidence.schema import CandidateFinding
from autofix_next.indexing.symbols import ImportRecord, SymbolTable
from autofix_next.parsing.tree_sitter import ParseResult

RULE_ID: str = "unused-import.intra-file"
RULE_VERSION: str = "v1"


def _normalize_import_text(raw_text: str) -> str:
    """Collapse all runs of whitespace in ``raw_text`` to a single space.

    Two imports that differ only in formatting (``from x import a,b`` vs
    ``from x import a, b`` vs ``from x import (\\n    a, b,\\n)``) must
    produce the same fingerprint so a reformatted file does not create a
    churn of "new" findings.
    """

    return " ".join(raw_text.split())


def _build_changed_slice(
    lines: list[str], start_line: int, end_line: int
) -> str:
    """Extract up to three-line context around the import.

    ``start_line`` / ``end_line`` are 1-indexed and inclusive. The window
    widens by one line on each side and is clamped to the file bounds.
    The result is the joined text (newline-separated) — callers embed
    this directly into :class:`EvidencePacket.changed_slice`.
    """

    total = len(lines)
    lo = max(0, start_line - 2)
    hi = min(total, end_line + 2)
    if lo >= hi:
        # Degenerate (empty file / out-of-range) — fall back to just the
        # import lines clamped to available content.
        lo = max(0, start_line - 1)
        hi = min(total, end_line)
    return "\n".join(lines[lo:hi])


def _emit_finding(
    record: ImportRecord, parse_result: ParseResult
) -> CandidateFinding:
    """Produce a :class:`CandidateFinding` for a single unused import."""

    normalized = _normalize_import_text(record.raw_text)
    finding_id = compute_finding_fingerprint(
        RULE_ID,
        parse_result.relpath,
        record.bound_name,
        normalized,
    )
    changed_slice = _build_changed_slice(
        parse_result.lines, record.start_line, record.end_line
    )
    return CandidateFinding(
        rule_id=RULE_ID,
        path=parse_result.relpath,
        symbol_name=record.bound_name,
        normalized_import=normalized,
        start_line=record.start_line,
        end_line=record.end_line,
        changed_slice=changed_slice,
        finding_id=finding_id,
    )


def analyze(
    parse_result: ParseResult, symbol_table: SymbolTable
) -> list[CandidateFinding]:
    """Return candidate findings for every unreferenced import.

    An import is considered used if its bound name appears anywhere in
    :attr:`SymbolTable.references` (i.e. outside of any import statement)
    *or* it is listed in a module-level ``__all__`` literal.

    Parameters
    ----------
    parse_result:
        Output of :func:`autofix_next.parsing.tree_sitter.parse_file`.
    symbol_table:
        Output of :func:`autofix_next.indexing.symbols.build_symbol_table`
        applied to the same ``parse_result``.

    Returns
    -------
    list[CandidateFinding]
        Zero or more findings in source order (same order as
        ``symbol_table.imports``). Each finding is a fully-populated
        :class:`CandidateFinding` whose ``finding_id`` is deterministic.
    """

    references = symbol_table.references
    exports: list[str] = symbol_table.all_exports or []
    export_set: set[str] = set(exports)

    findings: list[CandidateFinding] = []
    for record in symbol_table.imports:
        bound = record.bound_name
        if not bound:
            # Malformed record with an empty bound name — skip rather
            # than emit a finding whose fingerprint would be unstable.
            continue
        if bound in references:
            continue
        if bound in export_set:
            continue
        findings.append(_emit_finding(record, parse_result))
    return findings


__all__ = [
    "RULE_ID",
    "RULE_VERSION",
    "analyze",
]
