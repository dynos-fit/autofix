"""Security regression tests for autofix_next.migration validation gates.

Covers:
  sec-001: legacy evidence['file'] containing path-traversal sequences ('..',
           absolute paths, NUL bytes, Windows drive letters) is rejected.
  sec-002: legacy 'category' values that do not match the rule_id allowlist
           (^[a-z0-9][a-z0-9._-]*$) are rejected before projection.

Both findings (severity: critical, blocking) were raised by the
security-auditor in CHECKPOINT_AUDIT and remediated in repair-cycle 1.
"""
from __future__ import annotations

import json
from pathlib import Path

from autofix_next.migration import load_legacy_findings


def _write_findings(root: Path, entries: list[dict]) -> None:
    state_dir = root / ".autofix" / "state" / "current"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "findings.json").write_text(
        json.dumps({"findings": entries}), encoding="utf-8"
    )


def _legacy(finding_id: str, *, file: str = "ok.py", category: str = "rule-x") -> dict:
    return {
        "finding_id": finding_id,
        "status": "new",
        "category": category,
        "description": "test",
        "evidence": {"file": file, "line": 1, "symbol": "f"},
        "confidence_score": 1.0,
    }


# sec-001: evidence['file'] traversal rejection -----------------------------------

def test_sec001_relative_traversal_segment_rejected(tmp_path):
    logs: list[str] = []
    _write_findings(tmp_path, [_legacy("fp1", file="../../../etc/passwd")])
    out = load_legacy_findings(tmp_path, log=logs.append)
    assert out == []
    assert any("not a safe repo-relative path" in m for m in logs)


def test_sec001_absolute_posix_path_rejected(tmp_path):
    logs: list[str] = []
    _write_findings(tmp_path, [_legacy("fp1", file="/etc/passwd")])
    out = load_legacy_findings(tmp_path, log=logs.append)
    assert out == []
    assert any("not a safe repo-relative path" in m for m in logs)


def test_sec001_windows_drive_letter_rejected(tmp_path):
    logs: list[str] = []
    _write_findings(tmp_path, [_legacy("fp1", file="C:\\windows\\system32")])
    out = load_legacy_findings(tmp_path, log=logs.append)
    assert out == []


def test_sec001_backslash_traversal_rejected(tmp_path):
    logs: list[str] = []
    _write_findings(tmp_path, [_legacy("fp1", file="..\\evil")])
    out = load_legacy_findings(tmp_path, log=logs.append)
    assert out == []


def test_sec001_nul_byte_in_file_rejected(tmp_path):
    logs: list[str] = []
    _write_findings(tmp_path, [_legacy("fp1", file="ok\x00evil.py")])
    out = load_legacy_findings(tmp_path, log=logs.append)
    assert out == []


def test_sec001_safe_relative_path_accepted(tmp_path):
    _write_findings(tmp_path, [_legacy("fp1", file="src/sub/mod.py")])
    out = load_legacy_findings(tmp_path)
    assert len(out) == 1
    assert out[0].path == "src/sub/mod.py"


def test_sec001_empty_file_accepted(tmp_path):
    # Missing-evidence path falls back to file="" — must remain valid.
    entry = _legacy("fp1")
    entry.pop("evidence")
    _write_findings(tmp_path, [entry])
    out = load_legacy_findings(tmp_path)
    assert len(out) == 1
    assert out[0].path == ""


# sec-002: category / rule_id allowlist -------------------------------------------

def test_sec002_traversal_category_rejected(tmp_path):
    logs: list[str] = []
    _write_findings(tmp_path, [_legacy("fp1", category="../../evil")])
    out = load_legacy_findings(tmp_path, log=logs.append)
    assert out == []
    assert any("rule_id allowlist" in m for m in logs)


def test_sec002_uppercase_category_rejected(tmp_path):
    logs: list[str] = []
    _write_findings(tmp_path, [_legacy("fp1", category="UPPERCASE")])
    out = load_legacy_findings(tmp_path, log=logs.append)
    assert out == []


def test_sec002_leading_dot_category_rejected(tmp_path):
    logs: list[str] = []
    _write_findings(tmp_path, [_legacy("fp1", category=".hidden")])
    out = load_legacy_findings(tmp_path, log=logs.append)
    assert out == []


def test_sec002_empty_category_rejected(tmp_path):
    logs: list[str] = []
    _write_findings(tmp_path, [_legacy("fp1", category="")])
    out = load_legacy_findings(tmp_path, log=logs.append)
    assert out == []


def test_sec002_dot_separated_category_accepted(tmp_path):
    _write_findings(tmp_path, [_legacy("fp1", category="unused-import.intra-file")])
    out = load_legacy_findings(tmp_path)
    assert len(out) == 1
    assert out[0].rule_id == "unused-import.intra-file"


def test_sec002_underscore_dash_category_accepted(tmp_path):
    _write_findings(tmp_path, [_legacy("fp1", category="dead_code-rule.v2")])
    out = load_legacy_findings(tmp_path)
    assert len(out) == 1
