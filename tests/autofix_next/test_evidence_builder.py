"""Tests for autofix_next.evidence.builder and autofix_next.evidence.fingerprints.

Covers:
  AC #16 — EvidencePacket has exactly 7 top-level keys; analyzer_traces len == 1.
  AC #17 — canonical_json_bytes deterministic; grep shows no other json.dumps
           in the hash-input path.
  AC #18 — prompt_prefix_hash and finding_id follow their documented formulas
           and are length-64 lowercase hex strings (golden hashes).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_PACKET_KEYS = {
    "schema_version",
    "rule_id",
    "primary_symbol",
    "changed_slice",
    "supporting_symbols",
    "analyzer_traces",
    "prompt_prefix_hash",
}

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _sample_packet_dict() -> dict:
    return {
        "schema_version": "evidence_v1",
        "rule_id": "unused-import.intra-file",
        "primary_symbol": "sample.py::os",
        "changed_slice": "import os\n",
        "supporting_symbols": [],
        "analyzer_traces": [
            {
                "engine": "unused-import",
                "engine_version": "v1",
                "note": "bound name os has zero identifier references in file",
            }
        ],
    }


def test_evidence_packet_has_exactly_seven_keys() -> None:
    """AC #16: the packet JSON has exactly the 7 documented top-level keys."""
    from autofix_next.evidence.builder import build_packet

    packet = build_packet(
        rule_id="unused-import.intra-file",
        relpath="sample.py",
        symbol_name="os",
        normalized_import="import os",
        changed_slice="import os\n",
        analyzer_note="bound name os has zero identifier references in file",
    )
    packet_json = packet.to_json() if hasattr(packet, "to_json") else packet.__dict__

    assert set(packet_json.keys()) == EXPECTED_PACKET_KEYS, (
        f"unexpected key set: {set(packet_json.keys())!r}"
    )
    assert packet_json["schema_version"] == "evidence_v1"


def test_analyzer_traces_length_one() -> None:
    """AC #16: analyzer_traces has exactly one entry with keys
    {engine, engine_version, note}."""
    from autofix_next.evidence.builder import build_packet

    packet = build_packet(
        rule_id="unused-import.intra-file",
        relpath="sample.py",
        symbol_name="os",
        normalized_import="import os",
        changed_slice="import os\n",
        analyzer_note="bound name os has zero identifier references in file",
    )
    packet_json = packet.to_json() if hasattr(packet, "to_json") else packet.__dict__

    traces = packet_json["analyzer_traces"]
    assert isinstance(traces, list)
    assert len(traces) == 1
    assert set(traces[0].keys()) == {"engine", "engine_version", "note"}


def test_canonical_json_bytes_deterministic() -> None:
    """AC #17: canonical_json_bytes is sorted-keys, no whitespace,
    UTF-8, ensure_ascii=False, and deterministic across calls."""
    from autofix_next.evidence.fingerprints import canonical_json_bytes

    obj = {"b": 2, "a": 1, "c": {"y": True, "x": False}}
    encoded = canonical_json_bytes(obj)
    assert isinstance(encoded, (bytes, bytearray))
    assert canonical_json_bytes(obj) == encoded  # deterministic
    # No whitespace and sorted keys:
    assert encoded == b'{"a":1,"b":2,"c":{"x":false,"y":true}}'

    # ensure_ascii=False: non-ASCII characters are not escaped.
    unicode_obj = {"name": "café"}
    unicode_bytes = canonical_json_bytes(unicode_obj)
    assert "café".encode("utf-8") in unicode_bytes


def test_grep_no_other_json_dumps_in_hash_path() -> None:
    """AC #17: canonical_json_bytes is the only JSON-to-bytes serializer for
    hash inputs. All json.dumps usages under autofix_next/ must live in
    evidence/fingerprints.py, telemetry/sarif.py, or telemetry/events_log.py
    (SARIF and events.jsonl outputs are not hash inputs). Any json.dumps in
    any other module under autofix_next/ is forbidden."""
    pkg = REPO_ROOT / "autofix_next"
    assert pkg.is_dir(), f"autofix_next/ must exist: {pkg}"

    allowed = {
        pkg / "evidence" / "fingerprints.py",
        pkg / "telemetry" / "sarif.py",
        pkg / "telemetry" / "events_log.py",
    }

    # subprocess grep per task instruction.
    proc = subprocess.run(
        ["grep", "-R", "-n", "--include=*.py", "json.dumps", str(pkg)],
        capture_output=True,
        text=True,
    )
    # grep returns 1 when there are zero matches; that is a pass.
    if proc.returncode not in (0, 1):
        pytest.fail(f"grep failed: rc={proc.returncode} stderr={proc.stderr!r}")

    offending: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        # Format: "<path>:<lineno>:<match>"
        path_part = line.split(":", 1)[0]
        path = Path(path_part).resolve()
        if path not in {p.resolve() for p in allowed}:
            offending.append(line)

    assert not offending, (
        "json.dumps found outside the allowed hash/output modules "
        "(evidence/fingerprints.py, telemetry/sarif.py, telemetry/events_log.py):\n"
        + "\n".join(offending)
    )


def test_prompt_prefix_hash_formula() -> None:
    """AC #18: prompt_prefix_hash == sha256(canonical_json_bytes(packet_sans_hash))
    and is a length-64 lowercase hex string.  Golden hash is computed from
    the frozen sample packet."""
    from autofix_next.evidence.fingerprints import (
        canonical_json_bytes,
        compute_prompt_prefix_hash,
    )

    packet_sans_hash = _sample_packet_dict()
    expected = hashlib.sha256(canonical_json_bytes(packet_sans_hash)).hexdigest()
    got = compute_prompt_prefix_hash(packet_sans_hash)

    assert HEX64_RE.match(got), f"not lowercase hex-64: {got!r}"
    assert got == expected


def test_finding_id_formula() -> None:
    """AC #18: finding_id == sha256(f"{rule_id}|{relpath}|{symbol_name}|{normalized_import}")
    as lowercase hex-64. Golden hash is computed from the documented inputs."""
    from autofix_next.evidence.fingerprints import compute_finding_fingerprint

    rule_id = "unused-import.intra-file"
    relpath = "sample.py"
    symbol_name = "os"
    normalized_import = "import os"

    raw = f"{rule_id}|{relpath}|{symbol_name}|{normalized_import}".encode("utf-8")
    expected = hashlib.sha256(raw).hexdigest()

    got = compute_finding_fingerprint(
        rule_id=rule_id,
        relpath=relpath,
        symbol_name=symbol_name,
        normalized_import=normalized_import,
    )
    assert HEX64_RE.match(got), f"not lowercase hex-64: {got!r}"
    assert got == expected
