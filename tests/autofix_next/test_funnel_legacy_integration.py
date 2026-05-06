"""Integration tests for pipeline.py legacy-findings injection.

Covers:
  AC 11: policy key false → load_legacy_findings not called (scored_items unchanged)
  AC 12: policy key absent (default true) → scored_items grows by N legacy findings
  AC 10 (implicit): string "false" as policy value is truthy (non-empty string)

Patch target: `autofix_next.funnel.pipeline.load_legacy_findings`
(CON-03 from plan audit — from-import vs module-attr mismatch would
give false pass; always patch where the name is consumed).

These tests are authored TDD-first.  They will FAIL at import or at
collection time until:
  1. autofix_next/migration.py is created (load_legacy_findings exists)
  2. autofix_next/funnel/pipeline.py imports load_legacy_findings from migration
     so the patch target `autofix_next.funnel.pipeline.load_legacy_findings` is valid.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autofix_next.events.schema import ChangeSet
from autofix_next.evidence.schema import CandidateFinding
from autofix_next.funnel.pipeline import run_scan, ScanResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_candidate_finding(finding_id: str, path: str = "stub.py") -> CandidateFinding:
    return CandidateFinding(
        rule_id="unused-import",
        path=path,
        symbol_name="os",
        normalized_import="",
        start_line=1,
        end_line=1,
        changed_slice=f"{path}:1@os",
        finding_id=finding_id,
        analyzer_confidence=1.0,
    )


def _minimal_repo(tmp_path: Path) -> Path:
    """Create a minimal repo skeleton under tmp_path with a single Python file
    that has NO unused imports (so analyzer produces zero findings).
    Returns the repo root (tmp_path).
    """
    # Write a .py file with no unused imports so the analyzer contributes 0
    src_file = tmp_path / "clean_module.py"
    src_file.write_text("def hello():\n    return 42\n", encoding="utf-8")
    return tmp_path


def _write_policy(tmp_path: Path, policy: dict) -> None:
    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir(parents=True, exist_ok=True)
    (autofix_dir / "autofix-policy.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )


def _write_findings(tmp_path: Path, findings: list[dict]) -> None:
    state_dir = tmp_path / ".autofix" / "state" / "current"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "findings.json").write_text(
        json.dumps({"findings": findings}), encoding="utf-8"
    )


def _legacy_finding_dict(finding_id: str, status: str = "new") -> dict:
    return {
        "finding_id": finding_id,
        "status": status,
        "category": "unused-import",
        "description": "Unused import",
        "evidence": {"file": "clean_module.py", "line": 1, "symbol": "os"},
        "confidence_score": 1.0,
    }


def _run_minimal_scan(tmp_path: Path, policy: dict | None = None) -> ScanResult:
    """Run run_scan on a minimal repo with no analyzer findings."""
    root = _minimal_repo(tmp_path)
    changeset = ChangeSet(
        paths=("clean_module.py",),
        watcher_confidence="high",
    )
    return run_scan(root, changeset, scan_id="test-scan-001", policy=policy)


# ---------------------------------------------------------------------------
# AC 11: policy key false → load_legacy_findings NOT called
# ---------------------------------------------------------------------------

def test_legacy_disabled_skips_loader(tmp_path):
    # AC 11
    """When state_migration.legacy_findings_enabled is false (JSON boolean),
    load_legacy_findings must not be called at all.

    Patch target is autofix_next.funnel.pipeline.load_legacy_findings
    (per audit CON-03 — must be the reference inside the consuming module,
    not the source module).
    """
    _write_policy(tmp_path, {"state_migration": {"legacy_findings_enabled": False}})
    _write_findings(tmp_path, [_legacy_finding_dict("fp_should_be_ignored")])

    with patch(
        "autofix_next.funnel.pipeline.load_legacy_findings"
    ) as mock_loader:
        mock_loader.return_value = []
        policy_dict = {"state_migration": {"legacy_findings_enabled": False}}
        result = _run_minimal_scan(tmp_path, policy=policy_dict)

    mock_loader.assert_not_called(), (
        "load_legacy_findings must not be called when legacy_findings_enabled=false"
    )
    # Findings list should contain only analyzer findings (zero for this clean repo)
    assert len(result.findings) == 0


# ---------------------------------------------------------------------------
# AC 12: policy key absent → scored_items grows by N
# ---------------------------------------------------------------------------

def test_legacy_default_enabled_grows_scored_items(tmp_path):
    # AC 12
    """When state_migration.legacy_findings_enabled is absent (default true),
    a findings.json with 2 status=new entries must cause ScanResult.findings
    to contain 2 more entries than the analyzer-only baseline (which is 0
    for this clean-module fixture).
    """
    # No policy file → default true
    _write_findings(tmp_path, [
        _legacy_finding_dict("fp_legacy_1"),
        _legacy_finding_dict("fp_legacy_2"),
    ])

    legacy_finding_1 = _make_candidate_finding("fp_legacy_1", "clean_module.py")
    legacy_finding_2 = _make_candidate_finding("fp_legacy_2", "clean_module.py")

    # Patch the loader to return exactly 2 CandidateFinding instances, so we
    # don't depend on the production load_legacy_findings being fully wired yet.
    # The critical assertion is that the pipeline processes the returned list.
    with patch(
        "autofix_next.funnel.pipeline.load_legacy_findings"
    ) as mock_loader:
        mock_loader.return_value = [legacy_finding_1, legacy_finding_2]

        # Analyzer-only baseline: policy=None means no state_migration key → default true
        # We want the baseline WITH loader mocked to return zero to compare.
        # Strategy: run once with zero-return to get baseline, then with 2.

        # Baseline: mock returns []
        mock_loader.return_value = []
        baseline_result = _run_minimal_scan(tmp_path, policy=None)
        baseline_count = len(baseline_result.findings)

        # Reset: mock returns 2 findings
        mock_loader.return_value = [legacy_finding_1, legacy_finding_2]
        result_with_legacy = _run_minimal_scan(tmp_path, policy=None)

    mock_loader.assert_called(), "load_legacy_findings should be called when policy key is absent"

    assert len(result_with_legacy.findings) == baseline_count + 2, (
        f"Expected baseline ({baseline_count}) + 2 legacy findings = "
        f"{baseline_count + 2}, got {len(result_with_legacy.findings)}"
    )


# ---------------------------------------------------------------------------
# AC 10 / Implicit Requirement: string "false" is truthy (non-empty string)
# ---------------------------------------------------------------------------

def test_policy_value_string_truthy_enables(tmp_path):
    # AC 10 / Implicit Requirement (spec §Implicit Requirements)
    """When state_migration.legacy_findings_enabled is the string "false"
    (not the JSON boolean false), _legacy_migration_enabled must return True
    because bool("false") is True (non-empty string).

    Verified via: patch load_legacy_findings and assert it IS called when
    the policy value is the string "false".
    """
    _write_findings(tmp_path, [_legacy_finding_dict("fp_str_false")])

    # Policy with string "false" (not boolean false)
    policy_with_string_false = {
        "state_migration": {"legacy_findings_enabled": "false"}
    }

    with patch(
        "autofix_next.funnel.pipeline.load_legacy_findings"
    ) as mock_loader:
        mock_loader.return_value = []
        _run_minimal_scan(tmp_path, policy=policy_with_string_false)

    mock_loader.assert_called(), (
        "load_legacy_findings MUST be called when policy value is string 'false' "
        "(non-empty string is truthy in Python; only JSON boolean false disables)"
    )
