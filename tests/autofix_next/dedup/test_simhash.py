"""Unit tests for ``autofix_next.dedup.simhash``.

Covers AC #13 (64-bit SimHash over the canonical token stream) and AC #14
(``hamming_distance`` helper).
"""
from __future__ import annotations

from autofix_next.dedup.simhash import (
    ast_node_type_path,
    compute_simhash,
    hamming_distance,
    path_components,
    tokenize_rule_id,
)
from autofix_next.evidence.schema import CandidateFinding


def _make_finding(
    *,
    rule_id: str = "unused-import",
    path: str = "pkg/mod.py",
    symbol_name: str = "my_func",
    normalized_import: str = "os",
    start_line: int = 10,
    end_line: int = 10,
    changed_slice: str = "import os",
    finding_id: str = "fp_default",
) -> CandidateFinding:
    return CandidateFinding(
        rule_id,
        path,
        symbol_name,
        normalized_import,
        start_line,
        end_line,
        changed_slice,
        finding_id,
    )


def test_hamming_distance_known_pairs() -> None:
    """AC #14: hamming_distance returns bit_count of sig_a XOR sig_b.

    Four known pairs spanning all zeros, all ones, self-equal, and a
    2-bit / 2-bit interleaved pattern.
    """
    assert hamming_distance(0xFF, 0x00) == 8
    assert hamming_distance(0, 0) == 0
    assert hamming_distance(0xDEADBEEF, 0xDEADBEEF) == 0
    assert hamming_distance(0b1010, 0b0101) == 4


def test_tokenize_rule_id_splits_on_dash_and_underscore() -> None:
    """AC #13: tokenize_rule_id splits on both ``-`` and ``_``, lowercasing."""
    assert tokenize_rule_id("unused-import_intra-file") == [
        "unused",
        "import",
        "intra",
        "file",
    ]


def test_path_components_strips_py_suffix() -> None:
    """AC #13: path_components drops the trailing ``.py`` from the last segment.

    ``pkg/sub/mod.py`` → ``['pkg', 'sub', 'mod']`` so a finding in ``mod.py``
    and a hypothetical finding in a directory named ``mod`` tokenize the same.
    """
    assert path_components("pkg/sub/mod.py") == ["pkg", "sub", "mod"]


def test_ast_node_type_path_none_returns_empty() -> None:
    """AC #13: passing ``None`` as parse_result is a valid no-op.

    Callers that do not have a parsed tree still need to compute SimHash
    over the remaining two token streams.
    """
    assert ast_node_type_path(None, 1, 1) == []


def test_simhash_is_64_bit() -> None:
    """AC #13: compute_simhash returns an unsigned 64-bit integer.

    Smoke check ensures the signature never escapes the ``[0, 2**64)`` range
    regardless of token stream content.
    """
    finding = _make_finding()
    sig = compute_simhash(finding, None)
    assert 0 <= sig < 2**64


def test_simhash_deterministic() -> None:
    """Implicit-req: identical inputs produce identical signatures.

    SimHash is not permitted to leak any mutable state across invocations.
    """
    finding_a = _make_finding(finding_id="fp_a")
    finding_b = _make_finding(finding_id="fp_a")  # same data
    assert compute_simhash(finding_a, None) == compute_simhash(finding_b, None)


def test_simhash_token_stream_composition() -> None:
    """AC #13: token stream includes rule_id, ast_node_type_path, and path.

    Two findings differing only in ``path`` must produce different
    signatures (path_components contributes tokens). Likewise for ``rule_id``.
    """
    base = _make_finding(
        rule_id="unused-import",
        path="pkg/mod.py",
    )
    other_path = _make_finding(
        rule_id="unused-import",
        path="different/location.py",
    )
    other_rule = _make_finding(
        rule_id="dead-code",
        path="pkg/mod.py",
    )

    base_sig = compute_simhash(base, None)
    other_path_sig = compute_simhash(other_path, None)
    other_rule_sig = compute_simhash(other_rule, None)

    assert base_sig != other_path_sig
    assert base_sig != other_rule_sig
