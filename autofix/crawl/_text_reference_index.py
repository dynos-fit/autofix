"""Language-agnostic text-reference indexer for the bundle expander.

Augments the SCIP-based call graph with fuzzy textual references —
useful for any language without a SCIP indexer (Dart, HTML, Vue,
plain configs, generated docs) where ``CallGraph.symbols_in(path)``
returns nothing and bundles would otherwise degrade to singletons.

For each candidate path, scans content with a single alternation
regex matching all known candidate basenames at word boundaries.
Builds two indexes, both keyed and valued by **relative-path
strings** (matches SCIP's convention so the
``CallGraphPathAdapter`` can union both signals without
normalization).

* ``incoming[basename]`` → frozenset of relpaths whose content
  mentions that basename. Lookup is by basename: "who mentions me?"
* ``outgoing[relpath]`` → frozenset of relpaths whose basenames the
  given relpath's content mentions. Lookup is by relpath: "what
  do I mention?"

Documented limitations (not bugs):

* Multiple files sharing a basename in different directories all
  get associated with the same basename key. The bundle expander's
  per-cycle file/byte cap mitigates this — a fuzzy match still has
  to compete with other neighbors for the bundle's 5-file budget.
* Word-boundary regex matching does not understand quotes, syntax,
  or comments — ``foo`` inside a comment is a real match. The
  signal is intentionally fuzzy: false-positive context is cheaper
  than missed cross-file context.
* File contents are capped at :data:`MAX_INDEXED_BYTES` to bound
  the worst-case cycle cost on huge generated/lock files.
"""
from __future__ import annotations

import re
from pathlib import Path

# Cap per-file content at 500KB. Files larger than this are scanned
# only up to the cap — references in the tail are missed.
MAX_INDEXED_BYTES: int = 500_000


def build_text_reference_indexes(
    root: Path,
    candidates: list[Path],
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """Build (incoming, outgoing) text-reference indexes over candidates.

    Args:
        root: repo root used to compute each candidate's relpath.
        candidates: absolute paths to scan (typically every tracked
            file from ``git ls-files``).

    Returns:
        (incoming, outgoing) where:

        * incoming maps a basename string to a frozenset of relpath
          strings whose content mentions that basename.
        * outgoing maps a relpath string to a frozenset of relpath
          strings whose basenames appear in the given relpath's
          content.

        Missing keys are absent (use ``.get(key, frozenset())``).
        Both maps return ``frozenset`` so callers don't need to
        copy.
    """
    if not candidates:
        return {}, {}

    # basename → set of relpaths that have this basename. Multiple
    # paths can share a basename; we track all of them so a textual
    # match resolves to every same-named file in the repo.
    basename_to_relpaths: dict[str, set[str]] = {}
    rel_by_path: dict[Path, str] = {}
    for p in candidates:
        try:
            rel = str(p.relative_to(root))
        except ValueError:
            continue
        basename_to_relpaths.setdefault(p.name, set()).add(rel)
        rel_by_path[p] = rel

    if not basename_to_relpaths:
        return {}, {}

    # Single alternation regex over all known basenames. Sorted
    # longest-first so the engine prefers ``service_pb2.dart`` over
    # ``service.dart`` if both could match (rare in practice — they
    # almost never can given basename uniqueness).
    sorted_basenames = sorted(basename_to_relpaths.keys(), key=len, reverse=True)
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(b) for b in sorted_basenames) + r")\b"
    )

    incoming_work: dict[str, set[str]] = {}
    outgoing_work: dict[str, set[str]] = {}

    for path, self_rel in rel_by_path.items():
        try:
            content = path.read_text(errors="replace")
        except OSError:
            continue
        if len(content) > MAX_INDEXED_BYTES:
            content = content[:MAX_INDEXED_BYTES]

        matched_basenames = set(pattern.findall(content))
        # Drop self-references — picker.py mentioning "picker.py" in
        # its own docstring is not a cross-file edge.
        matched_basenames.discard(path.name)
        for bn in matched_basenames:
            for target_rel in basename_to_relpaths.get(bn, ()):
                if target_rel == self_rel:
                    continue
                outgoing_work.setdefault(self_rel, set()).add(target_rel)
                incoming_work.setdefault(bn, set()).add(self_rel)

    incoming = {k: frozenset(v) for k, v in incoming_work.items()}
    outgoing = {k: frozenset(v) for k, v in outgoing_work.items()}
    return incoming, outgoing


__all__ = ["build_text_reference_indexes", "MAX_INDEXED_BYTES"]
