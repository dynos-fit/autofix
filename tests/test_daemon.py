"""Tests for autofix.daemon module.

Covers acceptance criteria: 7, 8, 9, 10, 11, 12, 13, 19.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autofix.daemon import (
    daemon_start,
    daemon_status,
    daemon_stop,
    is_process_alive,
    read_pid_file,
    write_pid_file,
)


def _setup_repo(path: Path) -> Path:
    """Create a minimal repo with .autofix/ directory."""
    autofix_dir = path / ".autofix"
    autofix_dir.mkdir(parents=True)
    return path


def _write_pid_file(path: Path, pid: int) -> None:
    pid_file = path / ".autofix" / "daemon.pid"
    pid_file.write_text(str(pid))


# ---------------------------------------------------------------------------
# Criterion 7: PID file management
# ---------------------------------------------------------------------------

class TestPIDFileManagement:
    """Criterion 7: daemon writes PID to .autofix/daemon.pid."""

    def test_write_pid_file_creates_file(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        write_pid_file(repo, 12345)
        pid_file = repo / ".autofix" / "daemon.pid"
        assert pid_file.exists()
        assert pid_file.read_text().strip() == "12345"

    def test_read_pid_file_returns_pid(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        _write_pid_file(repo, 99999)
        assert read_pid_file(repo) == 99999

    def test_read_pid_file_returns_none_when_missing(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        assert read_pid_file(repo) is None

    def test_read_pid_file_returns_none_for_corrupt_file(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        (repo / ".autofix" / "daemon.pid").write_text("not-a-number")
        assert read_pid_file(repo) is None


# ---------------------------------------------------------------------------
# Criterion 7: double-fork daemon start
# ---------------------------------------------------------------------------

class TestDaemonStart:
    """Criterion 7: daemon_start forks to background and writes PID file."""

    def test_start_writes_pid_file(self, tmp_path: Path) -> None:
        """After daemon_start, a PID file should exist in .autofix/."""
        repo = _setup_repo(tmp_path / "repo")
        # Mock the fork to avoid actually forking. Simulate the child path.
        with (
            patch("autofix.daemon.os.fork", return_value=0),
            patch("autofix.daemon.os.setsid"),
            patch("autofix.daemon.os._exit"),
            patch("autofix.daemon.os.getpid", return_value=42),
            patch("autofix.daemon._daemon_loop"),
        ):
            daemon_start(root=repo)
        pid_file = repo / ".autofix" / "daemon.pid"
        assert pid_file.exists()
        assert pid_file.read_text().strip() == "42"

    def test_start_with_default_interval(self, tmp_path: Path) -> None:
        """Default interval should be 30 minutes (1800 seconds)."""
        repo = _setup_repo(tmp_path / "repo")
        loop_args: list = []
        with (
            patch("autofix.daemon.os.fork", return_value=0),
            patch("autofix.daemon.os.setsid"),
            patch("autofix.daemon.os._exit"),
            patch("autofix.daemon.os.getpid", return_value=42),
            patch("autofix.daemon._daemon_loop", side_effect=lambda **kw: loop_args.append(kw)),
        ):
            daemon_start(root=repo)
        assert loop_args[0]["interval_seconds"] == 1800


# ---------------------------------------------------------------------------
# Criterion 8: --interval flag with m/h suffixes
# ---------------------------------------------------------------------------

class TestIntervalParsing:
    """Criterion 8: --interval accepts m (minutes) and h (hours) suffixes."""

    def test_start_with_15m_interval(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        loop_args: list = []
        with (
            patch("autofix.daemon.os.fork", return_value=0),
            patch("autofix.daemon.os.setsid"),
            patch("autofix.daemon.os._exit"),
            patch("autofix.daemon.os.getpid", return_value=42),
            patch("autofix.daemon._daemon_loop", side_effect=lambda **kw: loop_args.append(kw)),
        ):
            daemon_start(root=repo, interval="15m")
        assert loop_args[0]["interval_seconds"] == 900

    def test_start_with_2h_interval(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        loop_args: list = []
        with (
            patch("autofix.daemon.os.fork", return_value=0),
            patch("autofix.daemon.os.setsid"),
            patch("autofix.daemon.os._exit"),
            patch("autofix.daemon.os.getpid", return_value=42),
            patch("autofix.daemon._daemon_loop", side_effect=lambda **kw: loop_args.append(kw)),
        ):
            daemon_start(root=repo, interval="2h")
        assert loop_args[0]["interval_seconds"] == 7200


# ---------------------------------------------------------------------------
# Criterion 9: refuse to start if daemon already running
# ---------------------------------------------------------------------------

class TestDoubleStartPrevention:
    """Criterion 9: daemon refuses to start if already running."""

    def test_refuses_when_pid_alive(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        _write_pid_file(repo, 12345)
        with patch("autofix.daemon.is_process_alive", return_value=True):
            result = daemon_start(root=repo)
        assert result.exit_code == 1
        assert "already running" in result.message.lower()

    def test_removes_stale_pid_and_starts(self, tmp_path: Path) -> None:
        """Implicit requirement: stale PID is cleaned up and start proceeds."""
        repo = _setup_repo(tmp_path / "repo")
        _write_pid_file(repo, 99999)
        with (
            patch("autofix.daemon.is_process_alive", return_value=False),
            patch("autofix.daemon.os.fork", return_value=0),
            patch("autofix.daemon.os.setsid"),
            patch("autofix.daemon.os._exit"),
            patch("autofix.daemon.os.getpid", return_value=42),
            patch("autofix.daemon._daemon_loop"),
        ):
            result = daemon_start(root=repo)
        # Stale PID file should have been replaced
        assert (repo / ".autofix" / "daemon.pid").read_text().strip() == "42"


# ---------------------------------------------------------------------------
# Criterion 10: daemon stop
# ---------------------------------------------------------------------------

class TestDaemonStop:
    """Criterion 10: daemon_stop sends SIGTERM, waits, removes PID file."""

    def test_stop_sends_sigterm_and_removes_pid(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        _write_pid_file(repo, 12345)
        with (
            patch("autofix.daemon.is_process_alive", side_effect=[True, False, False]),
            patch("autofix.daemon.os.kill") as mock_kill,
        ):
            result = daemon_stop(root=repo)
        mock_kill.assert_called_with(12345, signal.SIGTERM)
        assert result.exit_code == 0
        assert not (repo / ".autofix" / "daemon.pid").exists()

    def test_stop_with_no_daemon_exits_0(self, tmp_path: Path) -> None:
        """Stopping when no daemon is running should succeed gracefully."""
        repo = _setup_repo(tmp_path / "repo")
        result = daemon_stop(root=repo)
        assert result.exit_code == 0
        assert "not running" in result.message.lower() or "no daemon" in result.message.lower()

    def test_stop_with_stale_pid_cleans_up(self, tmp_path: Path) -> None:
        """If PID exists but process is dead, clean up and exit 0."""
        repo = _setup_repo(tmp_path / "repo")
        _write_pid_file(repo, 99999)
        with patch("autofix.daemon.is_process_alive", return_value=False):
            result = daemon_stop(root=repo)
        assert result.exit_code == 0
        assert not (repo / ".autofix" / "daemon.pid").exists()


# ---------------------------------------------------------------------------
# Criterion 11: daemon status
# ---------------------------------------------------------------------------

class TestDaemonStatus:
    """Criterion 11: daemon_status reports running state, PID, and uptime."""

    def test_status_running(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        _write_pid_file(repo, 12345)
        with patch("autofix.daemon.is_process_alive", return_value=True):
            result = daemon_status(root=repo)
        assert "running" in result.message.lower()
        assert "12345" in result.message

    def test_status_not_running(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        result = daemon_status(root=repo)
        assert "not running" in result.message.lower() or "stopped" in result.message.lower()

    def test_status_stale_pid(self, tmp_path: Path) -> None:
        """Stale PID should be reported as not running."""
        repo = _setup_repo(tmp_path / "repo")
        _write_pid_file(repo, 99999)
        with patch("autofix.daemon.is_process_alive", return_value=False):
            result = daemon_status(root=repo)
        assert "not running" in result.message.lower() or "stale" in result.message.lower()


# ---------------------------------------------------------------------------
# Criterion 12: SIGTERM graceful handling
# ---------------------------------------------------------------------------

class TestSIGTERMHandling:
    """Criterion 12: daemon handles SIGTERM gracefully, removes PID file."""

    def test_sigterm_sets_shutdown_event(self, tmp_path: Path) -> None:
        """The SIGTERM handler should set the shutdown event."""
        import threading

        from autofix.daemon import _make_sigterm_handler

        shutdown_event = threading.Event()
        handler = _make_sigterm_handler(shutdown_event)
        assert not shutdown_event.is_set()
        handler(signal.SIGTERM, None)
        assert shutdown_event.is_set()


# ---------------------------------------------------------------------------
# Criterion 13: logging with ISO 8601 timestamps
# ---------------------------------------------------------------------------

class TestDaemonLogging:
    """Criterion 13: daemon logs to .autofix/daemon.log with ISO 8601 timestamps."""

    def test_log_file_path(self, tmp_path: Path) -> None:
        """Daemon log should go to .autofix/daemon.log."""
        repo = _setup_repo(tmp_path / "repo")
        from autofix.daemon import get_log_path

        assert get_log_path(repo) == repo / ".autofix" / "daemon.log"

    def test_log_format_contains_iso_timestamp(self, tmp_path: Path) -> None:
        """Log lines should contain ISO 8601 timestamps."""
        import logging
        import re

        from autofix.daemon import setup_daemon_logging

        repo = _setup_repo(tmp_path / "repo")
        logger = setup_daemon_logging(repo, logger_name="test_daemon_log")
        logger.info("test message")

        log_content = (repo / ".autofix" / "daemon.log").read_text()
        # ISO 8601 pattern: YYYY-MM-DDTHH:MM:SS or similar
        iso_pattern = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
        assert re.search(iso_pattern, log_content), f"No ISO timestamp in: {log_content}"
        assert "test message" in log_content


# ---------------------------------------------------------------------------
# Criterion 19: config hot-reload on each scan cycle
# ---------------------------------------------------------------------------

class TestConfigHotReload:
    """Criterion 19: daemon re-reads config.json on each scan cycle."""

    def test_daemon_loop_reads_config_each_cycle(self, tmp_path: Path) -> None:
        """The daemon should read config.json at the start of each cycle."""
        import threading

        from autofix.daemon import _daemon_loop

        repo = _setup_repo(tmp_path / "repo")
        # Write initial config
        config_file = repo / ".autofix" / "config.json"
        config_file.write_text(json.dumps({"interval": "10m"}))

        shutdown_event = threading.Event()
        scan_calls: list[dict] = []

        def mock_scan(root, config):
            scan_calls.append(config.copy())
            # Shut down after first cycle
            shutdown_event.set()

        _daemon_loop(
            root=repo,
            interval_seconds=1,
            shutdown_event=shutdown_event,
            scan_fn=mock_scan,
        )

        assert len(scan_calls) >= 1
        # The config from config.json should have been read
        assert scan_calls[0].get("interval") == "10m"


# ---------------------------------------------------------------------------
# is_process_alive helper
# ---------------------------------------------------------------------------

class TestIsProcessAlive:
    """Test the is_process_alive helper used for PID checks."""

    def test_current_process_is_alive(self) -> None:
        assert is_process_alive(os.getpid()) is True

    def test_nonexistent_pid_is_not_alive(self) -> None:
        # PID 4000000 is almost certainly not running
        assert is_process_alive(4000000) is False

    def test_negative_pid_is_not_alive(self) -> None:
        assert is_process_alive(-1) is False
