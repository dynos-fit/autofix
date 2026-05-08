"""Scan-tooling failure raises typed exception (ARCH-011 AC-15)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autofix.workflow import VerifyScanFailed
from autofix.workflow.verify import run_verification


def test_scan_nonzero_exit_raises(tmp_path: Path) -> None:
    bad_scan = SimpleNamespace(
        exit_code=2,
        findings=[],
        sarif_path=None,
        scan_id="test",
        watcher_confidence=None,
    )
    with patch("autofix.workflow.verify._run_scan_core", return_value=bad_scan):
        with pytest.raises(VerifyScanFailed) as exc:
            run_verification(
                root=tmp_path,
                analyzer_set=None,
                applied_finding_ids=frozenset(),
                pre_finding_ids=frozenset(),
                log_dir=tmp_path,
                attempt=1,
            )
    assert "scan exited 2" in str(exc.value)


def test_verify_scan_failed_is_exception_subclass() -> None:
    assert issubclass(VerifyScanFailed, Exception)
