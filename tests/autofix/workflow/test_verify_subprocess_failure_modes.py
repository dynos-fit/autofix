"""subprocess failure-mode mapping (ARCH-011 AC-9)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from autofix.workflow.verify import _run_test_command


def test_pass_returns_true(tmp_path: Path) -> None:
    """Exit code 0 → test_passed=True."""
    log = tmp_path / "verify-1.log"
    result = _run_test_command(
        command=("python3", "-c", "import sys; sys.exit(0)"),
        cwd=tmp_path,
        timeout=10,
        log_path=log,
    )
    assert result is True
    assert log.exists()
    assert "exit_code=0" in log.read_text()


def test_nonzero_exit_returns_false(tmp_path: Path) -> None:
    """Non-zero exit → test_passed=False."""
    log = tmp_path / "verify-1.log"
    result = _run_test_command(
        command=("python3", "-c", "import sys; sys.exit(7)"),
        cwd=tmp_path,
        timeout=10,
        log_path=log,
    )
    assert result is False
    assert "exit_code=7" in log.read_text()


def test_filenotfound_returns_false(tmp_path: Path) -> None:
    """Missing executable → FileNotFoundError → test_passed=False."""
    log = tmp_path / "verify-1.log"
    result = _run_test_command(
        command=("definitely-not-a-real-binary-12345",),
        cwd=tmp_path,
        timeout=10,
        log_path=log,
    )
    assert result is False
    assert log.exists()
    assert "FileNotFoundError" in log.read_text()


def test_timeout_returns_false(tmp_path: Path) -> None:
    """TimeoutExpired → test_passed=False with log capture."""
    log = tmp_path / "verify-1.log"
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["sleep", "999"], timeout=1, output="partial", stderr=""
        )
        result = _run_test_command(
            command=("sleep", "999"),
            cwd=tmp_path,
            timeout=1,
            log_path=log,
        )
    assert result is False
    assert log.exists()
    assert "TimeoutExpired" in log.read_text()


def test_log_captures_stdout_and_stderr(tmp_path: Path) -> None:
    log = tmp_path / "verify-1.log"
    _run_test_command(
        command=(
            "python3",
            "-c",
            "import sys; print('hi-stdout'); print('hi-stderr', file=sys.stderr)",
        ),
        cwd=tmp_path,
        timeout=10,
        log_path=log,
    )
    text = log.read_text()
    assert "hi-stdout" in text
    assert "hi-stderr" in text
