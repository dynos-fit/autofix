"""Guardrail test: load_legacy_findings must not write to locked surfaces.

Covers AC 8: the bytes on disk for .autofix/state/current/findings.json
are identical before and after a load_legacy_findings call.

This test is authored TDD-first and FAILS at import until
autofix_next/migration.py is created.
"""
from __future__ import annotations

import hashlib
import json

from autofix_next.migration import load_legacy_findings


def _sha256_of(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_no_writes_to_locked_surfaces(tmp_path):
    # AC 8
    """load_legacy_findings performs zero writes to .autofix/state/current/findings.json.

    The bytes on disk for findings.json are hashed before and after the call
    and must be byte-identical.  Any write (even content-preserving rewrite)
    would change the mtime and would invalidate the byte-identity contract.
    """
    autofix_dir = tmp_path / ".autofix" / "state" / "current"
    autofix_dir.mkdir(parents=True)
    findings_file = autofix_dir / "findings.json"

    findings_payload = {
        "findings": [
            {
                "finding_id": "fp_locked_test",
                "status": "new",
                "category": "unused-import",
                "description": "Unused import os",
                "evidence": {"file": "a.py", "line": 5, "symbol": "os"},
                "confidence_score": 1.0,
            }
        ]
    }
    findings_file.write_text(json.dumps(findings_payload), encoding="utf-8")

    # Record the exact bytes before the call
    digest_before = _sha256_of(findings_file)
    raw_bytes_before = findings_file.read_bytes()

    # Execute the function under test
    result = load_legacy_findings(tmp_path)

    # Verify the bytes on disk are unchanged
    digest_after = _sha256_of(findings_file)
    raw_bytes_after = findings_file.read_bytes()

    assert digest_before == digest_after, (
        "findings.json was modified by load_legacy_findings "
        "(SHA-256 changed — locked surface violated)"
    )
    assert raw_bytes_before == raw_bytes_after, (
        "findings.json byte content changed after load_legacy_findings call"
    )

    # Sanity: the call should still have returned something meaningful
    assert len(result) == 1
    assert result[0].finding_id == "fp_locked_test"
