"""Tests for autofix_next.telemetry.sarif.emit_sarif.

Covers AC #10:
  - $schema == https://json.schemastore.org/sarif-2.1.0.json
  - version == 2.1.0
  - runs[0].tool.driver.name == "autofix-next"
  - every results[i].partialFingerprints["autofixNext/v1"] == finding_id
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _fake_finding(finding_id: str, relpath: str = "sample.py",
                  message: str = "Unused import") -> dict:
    """A minimal finding shape the emitter must accept. The concrete dataclass
    lives inside autofix_next; we pass a plain dict to keep this test decoupled
    from that shape — if the emitter expects an object, we upgrade this to a
    SimpleNamespace inside the test functions."""
    return {
        "finding_id": finding_id,
        "rule_id": "unused-import.intra-file",
        "message": message,
        "relpath": relpath,
        "start_line": 1,
        "level": "warning",
    }


def test_sarif_schema_and_driver(tmp_path: Path) -> None:
    """AC #10: $schema, version, and tool.driver.name are set correctly."""
    from autofix_next.telemetry.sarif import emit_sarif

    sarif_path = tmp_path / "findings.sarif"
    emit_sarif(
        scan_id="20260417T000000Z-abcdef01",
        findings=[_fake_finding("a" * 64)],
        sarif_path=sarif_path,
    )

    assert sarif_path.is_file()
    doc = json.loads(sarif_path.read_text(encoding="utf-8"))

    assert doc["$schema"] == "https://json.schemastore.org/sarif-2.1.0.json"
    assert doc["version"] == "2.1.0"
    assert doc["runs"][0]["tool"]["driver"]["name"] == "autofix-next"


def test_partial_fingerprints_match_finding_id(tmp_path: Path) -> None:
    """AC #10: results[i].partialFingerprints['autofixNext/v1'] equals the
    finding_id for every emitted result."""
    from autofix_next.telemetry.sarif import emit_sarif

    ids = ["a" * 64, "b" * 64, "c" * 64]
    findings = [_fake_finding(fid, relpath=f"mod{i}.py")
                for i, fid in enumerate(ids)]

    sarif_path = tmp_path / "findings.sarif"
    emit_sarif(
        scan_id="20260417T000000Z-abcdef01",
        findings=findings,
        sarif_path=sarif_path,
    )

    doc = json.loads(sarif_path.read_text(encoding="utf-8"))
    results = doc["runs"][0]["results"]
    assert len(results) == len(ids)
    for result, expected_fid in zip(results, ids):
        fps = result.get("partialFingerprints", {})
        assert fps.get("autofixNext/v1") == expected_fid, (
            f"partialFingerprint mismatch: expected {expected_fid!r}, got {fps!r}"
        )
