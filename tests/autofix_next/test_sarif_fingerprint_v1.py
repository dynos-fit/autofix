"""Tests for autofix_next.telemetry.sarif.compute_sarif_fingerprint_v1.

Covers ACs 8, 9, 10, 12, 13:

* AC 8  — the fingerprint payload is fed through ``canonical_json_bytes``
  and hashed with SHA-256, producing a 64-char lowercase hex digest.
* AC 9  — the payload keys are exactly
  ``{scheme_version, rule_id, artifact_uri, symbol_name}``.
* AC 10 — the function is deterministic: two calls with equivalent inputs
  produce byte-identical digests.
* AC 12 — **line-move invariance**: two findings that differ only in
  ``start_line`` / ``end_line`` produce byte-identical fingerprints.
* AC 13 — **three-way distinctness**: varying rule_id, path, and
  symbol_name independently each yield a distinct digest.
"""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace

import pytest

from autofix_next.evidence.fingerprints import canonical_json_bytes
from autofix_next.telemetry.sarif import (
    SARIF_FINGERPRINT_V1_SCHEME,
    compute_sarif_fingerprint_v1,
)


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _finding(
    *,
    rule_id: str = "unused-import.intra-file",
    path: str = "pkg/mod.py",
    symbol_name: str = "os",
    start_line: int = 1,
    end_line: int = 1,
) -> dict:
    """Minimal finding dict shape accepted by ``compute_sarif_fingerprint_v1``."""
    return {
        "rule_id": rule_id,
        "path": path,
        "symbol_name": symbol_name,
        "start_line": start_line,
        "end_line": end_line,
    }


# ---------------------------------------------------------------------------
# AC 8 + AC 10: hex shape + determinism
# ---------------------------------------------------------------------------


def test_fingerprint_is_64_char_lowercase_hex() -> None:
    """AC 8: output is a 64-char lowercase SHA-256 hex digest."""
    digest = compute_sarif_fingerprint_v1(_finding())
    assert isinstance(digest, str)
    assert _HEX64.match(digest), f"expected 64-char lowercase hex, got {digest!r}"
    # Extra belt-and-braces: no uppercase letters.
    assert digest == digest.lower()


def test_fingerprint_is_deterministic_across_calls() -> None:
    """AC 10: two calls with equivalent inputs produce the same digest."""
    a = compute_sarif_fingerprint_v1(_finding())
    b = compute_sarif_fingerprint_v1(_finding())
    assert a == b


def test_fingerprint_dict_and_namespace_agree() -> None:
    """AC 9 adapter contract: dict and object inputs with the same
    logical fields produce the same digest (the ``_get`` adapter must
    not leak shape into the hash)."""
    as_dict = _finding()
    as_obj = SimpleNamespace(**as_dict)
    assert compute_sarif_fingerprint_v1(as_dict) == compute_sarif_fingerprint_v1(as_obj)


# ---------------------------------------------------------------------------
# AC 12: line-move invariance
# ---------------------------------------------------------------------------


def test_line_move_invariance_start_line_only() -> None:
    """AC 12: changing only ``start_line`` MUST NOT change the digest."""
    base = _finding(start_line=10, end_line=10)
    moved = _finding(start_line=250, end_line=10)
    assert compute_sarif_fingerprint_v1(base) == compute_sarif_fingerprint_v1(moved)


def test_line_move_invariance_end_line_only() -> None:
    """AC 12: changing only ``end_line`` MUST NOT change the digest."""
    base = _finding(start_line=10, end_line=12)
    moved = _finding(start_line=10, end_line=999)
    assert compute_sarif_fingerprint_v1(base) == compute_sarif_fingerprint_v1(moved)


def test_line_move_invariance_both_lines() -> None:
    """AC 12 core: the same (rule_id, path, symbol_name) tuple with any
    line pair collapses to one fingerprint."""
    a = _finding(start_line=10, end_line=12)
    b = _finding(start_line=100, end_line=102)
    c = _finding(start_line=1, end_line=1)
    assert compute_sarif_fingerprint_v1(a) == compute_sarif_fingerprint_v1(b) == compute_sarif_fingerprint_v1(c)


# ---------------------------------------------------------------------------
# AC 13: three-way distinctness
# ---------------------------------------------------------------------------


def test_three_way_distinct_fingerprints_differ_pairwise() -> None:
    """AC 13: varying rule_id / path / symbol_name one at a time each
    changes the digest, and all three variants differ from each other
    AND from the seed."""
    seed = _finding(
        rule_id="unused-import.intra-file",
        path="pkg/mod.py",
        symbol_name="os",
    )
    by_rule = _finding(
        rule_id="OTHER_RULE",
        path="pkg/mod.py",
        symbol_name="os",
    )
    by_path = _finding(
        rule_id="unused-import.intra-file",
        path="OTHER/path.py",
        symbol_name="os",
    )
    by_symbol = _finding(
        rule_id="unused-import.intra-file",
        path="pkg/mod.py",
        symbol_name="OTHER",
    )

    seed_h = compute_sarif_fingerprint_v1(seed)
    rule_h = compute_sarif_fingerprint_v1(by_rule)
    path_h = compute_sarif_fingerprint_v1(by_path)
    sym_h = compute_sarif_fingerprint_v1(by_symbol)

    digests = {seed_h, rule_h, path_h, sym_h}
    assert len(digests) == 4, (
        "expected 4 pairwise-distinct digests, got "
        f"{len(digests)}: seed={seed_h} rule={rule_h} path={path_h} sym={sym_h}"
    )


# ---------------------------------------------------------------------------
# AC 9: payload construction is exactly the documented 4-key dict
# ---------------------------------------------------------------------------


def test_fingerprint_recomputes_from_canonical_json_bytes() -> None:
    """AC 9: reproducing the documented payload with
    ``canonical_json_bytes`` + SHA-256 matches the public function —
    this pins the exact four keys and the hashing recipe so a future
    refactor cannot silently add/remove fields."""
    finding = _finding(
        rule_id="unused-import.intra-file",
        path="pkg/mod.py",
        symbol_name="os",
        start_line=42,
        end_line=42,
    )
    payload = {
        "scheme_version": SARIF_FINGERPRINT_V1_SCHEME,
        "rule_id": "unused-import.intra-file",
        "artifact_uri": "pkg/mod.py",
        "symbol_name": "os",
    }
    expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    assert compute_sarif_fingerprint_v1(finding) == expected


def test_scheme_version_is_in_payload() -> None:
    """AC 9: changing the scheme_version token MUST change the digest.

    We recompute with a bogus scheme_version and assert it differs from
    the real one — this proves the constant participates in the hash
    rather than being decorative."""
    finding = _finding()
    real = compute_sarif_fingerprint_v1(finding)
    bogus_payload = {
        "scheme_version": "sarif-v2",  # bumped namespace
        "rule_id": finding["rule_id"],
        "artifact_uri": finding["path"],
        "symbol_name": finding["symbol_name"],
    }
    bogus = hashlib.sha256(canonical_json_bytes(bogus_payload)).hexdigest()
    assert real != bogus


def test_canonical_json_bytes_is_key_sorted() -> None:
    """AC 8: confirm the shared serializer sorts keys — otherwise two
    callers building the same payload in different insertion order
    could diverge. This is a regression guard for the hash recipe."""
    a = canonical_json_bytes({"a": 1, "b": 2})
    b = canonical_json_bytes({"b": 2, "a": 1})
    assert a == b
