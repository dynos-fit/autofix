"""Deterministic hashing primitives for EvidencePacket and CandidateFinding.

AC #17 & #18 pin the byte-level formulas. The module is intentionally tiny
and stdlib-only so it can be audited by eye and so its output is identical
on every supported Python version (3.11+).

Three public symbols:

* ``canonical_json_bytes`` — the SOLE JSON-to-bytes serializer for any hash
  input anywhere under ``autofix_next/``. Other modules MUST NOT call
  ``json.dumps`` on hash inputs; doing so is a contract violation enforced
  by ``tests/autofix_next/test_evidence_builder.py::test_grep_no_other_json_dumps_in_hash_path``.
* ``compute_prompt_prefix_hash`` — sha256 of ``canonical_json_bytes`` of the
  packet dict with the ``prompt_prefix_hash`` key removed (AC #18).
* ``compute_finding_fingerprint`` — sha256 of the four pipe-joined
  identifying fields of a finding (AC #18).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(obj: Any) -> bytes:
    """Serialize ``obj`` to the canonical byte form used for every hash input.

    Parameters are fixed and MUST NOT change without bumping
    ``evidence.schema.SCHEMA_VERSION``:

    * ``sort_keys=True`` — dict key order is not a hash input.
    * ``separators=(",", ":")`` — no whitespace; byte-stable.
    * ``ensure_ascii=False`` — Unicode characters are emitted as UTF-8
      byte sequences rather than ``\\uXXXX`` escapes, so two equivalent
      string values with different source encodings still hash equal.
    * UTF-8 encoding — the bytes fed to ``hashlib.sha256`` are explicit
      UTF-8, not the platform default.

    This is the SOLE serializer for hash inputs under ``autofix_next/``.
    Any other ``json.dumps`` call on a value that later feeds into a hash
    is a contract violation.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_prompt_prefix_hash(
    packet_dict_without_prompt_prefix_hash: dict,
) -> str:
    """Return the 64-char lowercase hex sha256 of the packet-minus-hash (AC #18).

    The caller is responsible for passing a dict that does NOT yet contain
    the ``prompt_prefix_hash`` key. Including the key would create a
    circular dependency (the hash depends on itself).
    """
    digest = hashlib.sha256(
        canonical_json_bytes(packet_dict_without_prompt_prefix_hash)
    ).hexdigest()
    return digest


def compute_finding_fingerprint(
    rule_id: str,
    relpath: str,
    symbol_name: str,
    normalized_import: str,
) -> str:
    """Return the stable ``finding_id`` for a candidate finding (AC #18).

    The four identifying fields are joined by ``|`` and UTF-8 encoded;
    callers are responsible for ensuring ``relpath`` is repo-relative and
    ``normalized_import`` has been canonicalized (whitespace/aliases
    stripped). No escaping is performed: pipes inside any field would
    produce a collision — which is acceptable because none of the four
    fields may contain a pipe by construction.
    """
    raw = f"{rule_id}|{relpath}|{symbol_name}|{normalized_import}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "canonical_json_bytes",
    "compute_prompt_prefix_hash",
    "compute_finding_fingerprint",
]
