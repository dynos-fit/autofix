"""Unresolved-set and new-finding count math (ARCH-011 AC-11, AC-12)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autofix.workflow.verify import run_verification


def _stub_scan(*finding_ids: str) -> object:
    findings = [
        SimpleNamespace(finding_id=fid) for fid in finding_ids
    ]
    return SimpleNamespace(
        exit_code=0,
        findings=findings,
        sarif_path=None,
        scan_id="test",
        watcher_confidence=None,
    )


def test_unresolved_intersects_applied_and_post(tmp_path: Path) -> None:
    """Applied finding still in post-scan results → unresolved."""
    with patch(
        "autofix.workflow.verify._run_scan_core",
        return_value=_stub_scan("F1", "F3"),
    ):
        result = run_verification(
            root=tmp_path,
            analyzer_set=None,
            applied_finding_ids=frozenset({"F1", "F2"}),
            pre_finding_ids=frozenset({"F1", "F2", "F3"}),
            log_dir=tmp_path,
            attempt=1,
        )

    # F1 was applied AND still present → unresolved.
    # F2 was applied AND no longer present → resolved.
    assert result.unresolved_finding_ids == frozenset({"F1"})
    assert result.regressed is True


def test_new_finding_count_excludes_pre_existing(tmp_path: Path) -> None:
    """Findings present pre-scan don't count as new."""
    with patch(
        "autofix.workflow.verify._run_scan_core",
        return_value=_stub_scan("F1", "F2", "F_NEW"),
    ):
        result = run_verification(
            root=tmp_path,
            analyzer_set=None,
            applied_finding_ids=frozenset(),
            pre_finding_ids=frozenset({"F1", "F2"}),
            log_dir=tmp_path,
            attempt=1,
        )

    assert result.new_finding_count == 1
    assert "F_NEW" in result.post_finding_ids
    assert result.regressed is True


def test_clean_run_no_regression(tmp_path: Path) -> None:
    with patch(
        "autofix.workflow.verify._run_scan_core",
        return_value=_stub_scan(),
    ):
        result = run_verification(
            root=tmp_path,
            analyzer_set=None,
            applied_finding_ids=frozenset({"F1"}),
            pre_finding_ids=frozenset({"F1"}),
            log_dir=tmp_path,
            attempt=1,
        )

    assert result.unresolved_finding_ids == frozenset()
    assert result.new_finding_count == 0
    # No marker → test_passed=None, no regression flag from tests.
    assert result.regressed is False


def test_post_finding_ids_sorted(tmp_path: Path) -> None:
    """Determinism: post_finding_ids returned sorted."""
    with patch(
        "autofix.workflow.verify._run_scan_core",
        return_value=_stub_scan("F-z", "F-a", "F-m"),
    ):
        result = run_verification(
            root=tmp_path,
            analyzer_set=None,
            applied_finding_ids=frozenset(),
            pre_finding_ids=frozenset({"F-z", "F-a", "F-m"}),
            log_dir=tmp_path,
            attempt=1,
        )

    assert result.post_finding_ids == ("F-a", "F-m", "F-z")
