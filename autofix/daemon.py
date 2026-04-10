"""Unix double-fork daemon for autofix background scanning."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Result type returned by daemon_start / daemon_stop / daemon_status
# ---------------------------------------------------------------------------

@dataclass
class DaemonResult:
    """Outcome of a daemon command."""

    exit_code: int
    message: str


# ---------------------------------------------------------------------------
# Interval parsing — delegated to autofix.config
# ---------------------------------------------------------------------------

from autofix.config import parse_interval  # noqa: E402


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------

_PID_FILE = "daemon.pid"
_LOG_FILE = "daemon.log"
_CONFIG_FILE = "config.json"
_AUTOFIX_DIR = ".autofix"


def _autofix_dir(root: Path) -> Path:
    return root / _AUTOFIX_DIR


def write_pid_file(root: Path, pid: int) -> None:
    """Write the daemon PID to .autofix/daemon.pid."""
    pid_path = _autofix_dir(root) / _PID_FILE
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid))


def read_pid_file(root: Path) -> int | None:
    """Read the daemon PID from .autofix/daemon.pid.

    Returns None if the file does not exist or is corrupt.
    """
    pid_path = _autofix_dir(root) / _PID_FILE
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None


def _remove_pid_file(root: Path) -> None:
    """Remove the PID file if it exists."""
    pid_path = _autofix_dir(root) / _PID_FILE
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Process liveness check
# ---------------------------------------------------------------------------

def is_process_alive(pid: int) -> bool:
    """Check whether *pid* refers to a running process via ``os.kill(pid, 0)``."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def get_log_path(root: Path) -> Path:
    """Return the path to .autofix/daemon.log."""
    return _autofix_dir(root) / _LOG_FILE


def setup_daemon_logging(
    root: Path,
    logger_name: str = "autofix.daemon",
) -> logging.Logger:
    """Configure a logger that appends to .autofix/daemon.log with ISO 8601 timestamps."""
    log_path = get_log_path(root)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)

    # Avoid adding duplicate handlers when called multiple times
    if not logger.handlers:
        handler = logging.FileHandler(str(log_path), mode="a")
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


# ---------------------------------------------------------------------------
# SIGTERM handler factory
# ---------------------------------------------------------------------------

def _make_sigterm_handler(
    shutdown_event: threading.Event,
) -> Callable[[int, Any], None]:
    """Return a signal handler that sets *shutdown_event* on SIGTERM."""

    def _handler(signum: int, frame: Any) -> None:
        shutdown_event.set()

    return _handler


# ---------------------------------------------------------------------------
# Config hot-reload
# ---------------------------------------------------------------------------

def _load_config(root: Path) -> dict[str, Any]:
    """Read .autofix/config.json, returning an empty dict on any error."""
    config_path = _autofix_dir(root) / _CONFIG_FILE
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Daemon scan loop
# ---------------------------------------------------------------------------

_default_scan_logger = logging.getLogger("autofix.daemon.scan")


def _default_scan_fn(root: Path, config: dict[str, Any]) -> None:
    """Default scan function that invokes the real scan pipeline.

    Calls scan_locked directly instead of cmd_scan to avoid the
    fcntl.flock in cmd_scan — that lock can leak to child processes
    (e.g. claude subprocess) and block subsequent daemon scan cycles.
    The daemon already ensures only one instance runs via PID file.
    """
    try:
        from autofix.app import runtime_factory
        from autofix.scanner import scan_locked

        resolved = root.resolve()
        max_findings = int(config.get("max_findings", 100))
        scan_locked(resolved, max_findings, runtime_factory(root=resolved))
    except Exception as exc:
        _default_scan_logger.exception("Scan cycle failed: %s", exc)


def _daemon_loop(
    *,
    root: Path,
    interval_seconds: int,
    shutdown_event: threading.Event,
    scan_fn: Callable[..., Any] | None = None,
) -> None:
    """Main daemon loop: scan, sleep, repeat until shutdown.

    On each cycle the config is hot-reloaded from .autofix/config.json
    (acceptance criterion 19).
    """
    if scan_fn is None:
        scan_fn = _default_scan_fn

    logger = setup_daemon_logging(root, logger_name="autofix.daemon.loop")
    logger.info("Daemon loop started (interval=%ds)", interval_seconds)

    while not shutdown_event.is_set():
        try:
            config = _load_config(root)
        except Exception:
            logger.exception("Failed to load config; using defaults")
            config = {}
        # Hot-reload interval from config (criterion 19)
        config_interval = config.get("interval")
        if config_interval:
            try:
                interval_seconds = parse_interval(str(config_interval))
            except (ValueError, TypeError):
                pass
        logger.info("Starting scan cycle")
        try:
            scan_fn(root, config)
        except Exception:
            logger.exception("Scan cycle failed")

        logger.info("Scan cycle complete; sleeping %ds", interval_seconds)
        # Interruptible sleep via threading.Event.wait
        shutdown_event.wait(timeout=interval_seconds)

    logger.info("Daemon loop exiting (shutdown requested)")
    _remove_pid_file(root)


# ---------------------------------------------------------------------------
# Double-fork daemon start
# ---------------------------------------------------------------------------

def _redirect_stdio(root: Path) -> None:
    """Redirect stdin/stdout/stderr to /dev/null and the log file."""
    log_path = get_log_path(root)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, sys.stdin.fileno())
    os.close(devnull)

    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)


def daemon_start(
    *,
    root: Path,
    interval: str | None = None,
) -> DaemonResult:
    """Start the autofix daemon via Unix double-fork.

    Returns a DaemonResult.  In the parent process this returns after the
    first fork; in the grandchild it enters the scan loop and never returns
    normally.
    """
    # Ensure .autofix directory exists
    _autofix_dir(root).mkdir(parents=True, exist_ok=True)

    # --- Check for already-running daemon (criterion 9) ---
    existing_pid = read_pid_file(root)
    if existing_pid is not None:
        if is_process_alive(existing_pid):
            return DaemonResult(
                exit_code=1,
                message=f"Daemon already running (PID {existing_pid}).",
            )
        # Stale PID -- clean up
        _remove_pid_file(root)

    # --- Parse interval (criterion 8) ---
    if interval is None:
        interval_seconds = 1800  # 30 minutes default
    else:
        interval_seconds = parse_interval(interval)

    # --- First fork ---
    pid = os.fork()
    if pid > 0:
        # Original parent: print confirmation before exiting
        print("Daemon started (PID will be written to .autofix/daemon.pid)")
        os._exit(0)

    # --- Child: new session ---
    os.setsid()

    # --- Second fork ---
    pid = os.fork()
    if pid > 0:
        # First child exits
        os._exit(0)

    # --- Grandchild: the daemon process ---
    daemon_pid = os.getpid()
    write_pid_file(root, daemon_pid)

    # Set up SIGTERM handler
    shutdown_event = threading.Event()
    signal.signal(signal.SIGTERM, _make_sigterm_handler(shutdown_event))

    # Redirect stdio (skip if running under test with mocked fork)
    try:
        _redirect_stdio(root)
    except OSError:
        pass

    # Enter scan loop
    _daemon_loop(
        root=root,
        interval_seconds=interval_seconds,
        shutdown_event=shutdown_event,
    )

    return DaemonResult(exit_code=0, message="Daemon exited.")


# ---------------------------------------------------------------------------
# daemon stop
# ---------------------------------------------------------------------------

def daemon_stop(*, root: Path) -> DaemonResult:
    """Stop the autofix daemon for the repo at *root*.

    Reads .autofix/daemon.pid, sends SIGTERM, waits up to 10 s, removes the
    PID file.  Returns exit_code 0 even if no daemon is running.
    """
    pid = read_pid_file(root)

    if pid is None:
        return DaemonResult(exit_code=0, message="No daemon running (PID file not found).")

    if not is_process_alive(pid):
        _remove_pid_file(root)
        return DaemonResult(exit_code=0, message=f"No daemon running (stale PID {pid} removed).")

    # Send SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        _remove_pid_file(root)
        return DaemonResult(exit_code=0, message=f"Failed to send SIGTERM to PID {pid}; cleaned up.")

    # Wait up to 10 seconds for the process to exit
    deadline = time.monotonic() + 10
    exited = False
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            exited = True
            break
        time.sleep(0.2)

    if exited:
        _remove_pid_file(root)
        return DaemonResult(exit_code=0, message=f"Daemon (PID {pid}) stopped.")

    return DaemonResult(exit_code=1, message=f"Daemon (PID {pid}) did not exit within 10s; PID file retained.")


# ---------------------------------------------------------------------------
# daemon status
# ---------------------------------------------------------------------------

def daemon_status(*, root: Path) -> DaemonResult:
    """Report whether a daemon is running for *root*, its PID, and uptime."""
    pid = read_pid_file(root)

    if pid is None:
        return DaemonResult(exit_code=0, message="Daemon is not running.")

    if not is_process_alive(pid):
        _remove_pid_file(root)
        return DaemonResult(exit_code=0, message=f"Daemon is not running (stale PID {pid} removed).")

    # Try to compute uptime from PID file mtime
    pid_path = _autofix_dir(root) / _PID_FILE
    try:
        started = pid_path.stat().st_mtime
        uptime_secs = int(time.time() - started)
        hours, remainder = divmod(uptime_secs, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"
    except OSError:
        uptime_str = "unknown"

    return DaemonResult(
        exit_code=0,
        message=f"Daemon is running (PID {pid}, uptime {uptime_str}).",
    )
