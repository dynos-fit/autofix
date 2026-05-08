"""Skip path: no marker, no config → test_passed=None (ARCH-011 AC-10)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from autofix.workflow.verify import run_verification


def _stub_scan_result(findings: list[object] | None = None) -> object:
    class _R:
        exit_code = 0
        sarif_path = None
        scan_id = "test"
        watcher_confidence = None

    r = _R()
    r.findings = findings or []
    return r


def test_no_marker_no_config_skips_tests(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    with patch("autofix.workflow.verify._run_scan_core", return_value=_stub_scan_result()):
        result = run_verification(
            root=tmp_path,
            analyzer_set=None,
            applied_finding_ids=frozenset(),
            pre_finding_ids=frozenset(),
            log_dir=log_dir,
            attempt=1,
        )

    assert result.test_passed is None
    assert result.test_runner is None
    assert result.test_command is None
    assert result.test_log_path is None
    assert result.regressed is False
    assert result.new_finding_count == 0
    assert result.unresolved_finding_ids == frozenset()
