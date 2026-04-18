"""64-bit SimHash over a deterministic token stream (AC #13, AC #14).

This module is pure Python stdlib: the only hashing dependency is
:mod:`hashlib` (blake2b). External SimHash libraries are deliberately
avoided so the dedup tier-2 signature remains reproducible across
machines with zero additional wheels.

Token stream (exact, per AC #13)::

    tokenize_rule_id(rule_id)
  + ast_node_type_path(parse_result, start_line, end_line)
  + path_components(path)

Each token is hashed to 64 bits with ``blake2b(digest_size=8)``. The
canonical SimHash accumulator has 64 signed counters: for each bit of
each token hash we add +1 on a 1-bit and -1 on a 0-bit. The final
signature bit is 1 where the counter is positive, 0 otherwise.

Tokens are unweighted (uniform weight 1). Callers pass an already-parsed
:class:`~autofix_next.parsing.tree_sitter.ParseResult` so this module
never re-parses source text.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from autofix_next.evidence.schema import CandidateFinding


_SIG_BITS = 64


def tokenize_rule_id(rule_id: str) -> list[str]:
    """Break a ``rule_id`` into a human-form token list.

    ``rule_id`` is snake_case or kebab-case (for example
    ``unused-import-intra-file`` or ``unused_import_intra_file``). We
    split on both ``-`` and ``_``, lowercase each piece, and drop empty
    tokens produced by leading/trailing/duplicate separators.

    A non-string ``rule_id`` or an empty string yields ``[]`` so callers
    can safely feed partially-constructed findings.
    """

    if not isinstance(rule_id, str) or not rule_id:
        return []
    # Replace underscores with hyphens then split once so we do not need
    # to call split twice and build an intermediate list per char.
    pieces = rule_id.replace("_", "-").split("-")
    return [p.lower() for p in pieces if p]


def path_components(path: str) -> list[str]:
    """Split a repo-relative POSIX path into its components.

    The final ``.py`` suffix (if any) is stripped from the last
    component so two findings in ``pkg/mod.py`` and ``pkg/mod`` land on
    the same path-tokens. Empty segments (from leading ``/`` or
    duplicated separators) are dropped.

    Non-string or empty ``path`` yields ``[]``.
    """

    if not isinstance(path, str) or not path:
        return []
    # Normalize Windows-style separators to POSIX so callers that hand
    # us a back-slashed path on macOS CI still get a consistent token
    # stream. We intentionally do not use pathlib here: we want plain
    # string semantics, not OS-specific resolution.
    normalized = path.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    if not parts:
        return []
    last = parts[-1]
    if last.endswith(".py") and len(last) > 3:
        parts[-1] = last[:-3]
    return parts


def ast_node_type_path(
    parse_result: Any, start_line: int, end_line: int
) -> list[str]:
    """Return the top-down chain of tree-sitter node types covering a span.

    Walks ``parse_result.tree.root_node`` downward, at each step picking
    the first child whose byte/line range fully contains the
    1-indexed inclusive span ``[start_line, end_line]``. Each visited
    node's ``.type`` is appended, producing an ancestor-chain list such
    as ``['module', 'import_from_statement', 'import_list']``.

    Accepts ``None`` (returns ``[]``) so tests and callers that do not
    have a parsed tree can still compose the other two token streams.

    The underlying tree must already be parsed — we never parse source
    text here.

    Parameters
    ----------
    parse_result:
        A :class:`~autofix_next.parsing.tree_sitter.ParseResult`
        instance, or a compatible object that exposes either
        ``tree.root_node`` or ``root_node``. ``None`` is accepted.
    start_line, end_line:
        1-indexed inclusive line range to locate in the tree.

    Returns
    -------
    list[str]
        Top-down chain of node ``.type`` strings. Empty when
        ``parse_result`` is ``None`` or the root node cannot be
        resolved.
    """

    if parse_result is None:
        return []

    # Resolve the root node tolerantly: prefer ``tree.root_node`` (the
    # concrete shape used by :class:`ParseResult`) and fall back to a
    # direct ``root_node`` attribute for other duck-typed objects used
    # in tests.
    root = None
    tree = getattr(parse_result, "tree", None)
    if tree is not None:
        root = getattr(tree, "root_node", None)
    if root is None:
        root = getattr(parse_result, "root_node", None)
    if root is None:
        return []

    # Convert the 1-indexed inclusive span to tree-sitter's 0-indexed
    # rows. We keep end as inclusive in the 0-indexed domain.
    try:
        target_lo = int(start_line) - 1
        target_hi = int(end_line) - 1
    except (TypeError, ValueError):
        return []
    if target_hi < target_lo:
        target_lo, target_hi = target_hi, target_lo

    def _contains(node: Any) -> bool:
        start_point = getattr(node, "start_point", None)
        end_point = getattr(node, "end_point", None)
        if start_point is None or end_point is None:
            return False
        try:
            n_lo = int(start_point[0])
            n_hi = int(end_point[0])
        except (TypeError, ValueError, IndexError):
            return False
        return n_lo <= target_lo and n_hi >= target_hi

    chain: list[str] = []
    node: Any = root
    while node is not None:
        node_type = getattr(node, "type", None)
        if isinstance(node_type, str):
            chain.append(node_type)
        children = getattr(node, "children", None) or []
        next_node: Any = None
        for child in children:
            if _contains(child):
                next_node = child
                break
        node = next_node
    return chain


def _hash64(token: str) -> int:
    """Deterministic 64-bit hash of a single token via stdlib blake2b."""

    return int.from_bytes(
        hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(),
        "big",
    )


def compute_simhash(
    finding: "CandidateFinding", parse_result: Any = None
) -> int:
    """Return a 64-bit SimHash signature for ``finding``.

    Token stream (exact, per AC #13)::

        tokenize_rule_id(finding.rule_id)
      + ast_node_type_path(parse_result, finding.start_line, finding.end_line)
      + path_components(finding.path)

    Each token is hashed with :func:`_hash64` (blake2b, 8-byte digest).
    A 64-slot signed accumulator receives +1 for every 1-bit and -1 for
    every 0-bit of every token hash. The final signature bit is 1 where
    the accumulator slot is strictly positive, otherwise 0.

    Empty token list → returns 0. The result is always in
    ``[0, 2**64)``.
    """

    tokens: list[str] = []
    tokens.extend(tokenize_rule_id(finding.rule_id))
    tokens.extend(
        ast_node_type_path(parse_result, finding.start_line, finding.end_line)
    )
    tokens.extend(path_components(finding.path))

    if not tokens:
        return 0

    accumulator = [0] * _SIG_BITS
    for token in tokens:
        h = _hash64(token)
        for bit in range(_SIG_BITS):
            if (h >> bit) & 1:
                accumulator[bit] += 1
            else:
                accumulator[bit] -= 1

    signature = 0
    for bit in range(_SIG_BITS):
        if accumulator[bit] > 0:
            signature |= 1 << bit
    return signature


def hamming_distance(sig_a: int, sig_b: int) -> int:
    """Return the bit-count of ``sig_a ^ sig_b`` (AC #14).

    Uses :meth:`int.bit_count` (Python 3.10+) which is the stdlib-canonical
    popcount. Accepts any Python ints — callers typically pass 64-bit
    signatures produced by :func:`compute_simhash`.
    """

    return (int(sig_a) ^ int(sig_b)).bit_count()


__all__ = [
    "tokenize_rule_id",
    "path_components",
    "ast_node_type_path",
    "compute_simhash",
    "hamming_distance",
]
