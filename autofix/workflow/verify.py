"""VERIFYING-state primitive for the autofix workflow loop (ARCH-011).

Pure function ``run_verification`` that:

1. Detects the project's test runner via marker files (or reads the
   project-level configuration file override).
2. Runs the resolved test command via ``subprocess.run`` with timeout,
   capturing stdout/stderr to a log file under the run-specific
   directory.
3. Re-scans the working tree via ``_run_scan_core`` to surface new
   findings and identify which applied finding-ids are still present.
4. Returns a :class:`VerifyResult` carrying the test outcome plus the
   set/count math the orchestrator needs to drive
   VERIFYING → {DONE, RETRY, FAILED}.

All numeric and string constants live in
:mod:`autofix.workflow.verify_constants`. The body of this module
references those constants by name; the no-magic-numbers grep test
treats inline literals as a binding contract violation.

The function is pure (returns a result object; the caller drives
the state machine). Scan-tooling failure raises
:class:`VerifyScanFailed` (typed exception, distinct from a test
regression — the orchestrator maps it to the FAILED transition).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from autofix.cli.scan_command import _run_scan_core
from autofix.workflow.verify_constants import (
    CONFIG_PATH_RELATIVE,
    DEFAULT_TIMEOUT_SECONDS,
    SUBPROCESS_EXIT_CODE_OK,
    TEST_RUNNER_DETECTION_ORDER,
    VERIFY_LOG_FILENAME_TEMPLATE,
)


class VerifyResult(NamedTuple):
    """Outcome of one VERIFYING iteration (ARCH-011 AC-3)."""

    test_passed: bool | None
    test_runner: str | None
    test_command: tuple[str, ...] | None
    test_log_path: Path | None
    new_finding_count: int
    unresolved_finding_ids: frozenset[str]
    post_finding_ids: tuple[str, ...]
    regressed: bool


class VerifyScanFailed(Exception):
    """Raised when the post-apply re-scan tooling itself fails.

    Distinct from a test regression: the orchestrator maps this to the
    FAILED state transition rather than RETRY.
    """


def _finding_id(f: object) -> str:
    """Best-effort finding-id extraction (mirrors run_command)."""
    return str(getattr(f, "finding_id", None) or getattr(f, "id", None) or "")


def _detect_runner(root: Path) -> tuple[str | None, tuple[str, ...] | None]:
    """Walk the priority-tuple; first marker present wins (AC-7)."""
    for marker, runner_name, command in TEST_RUNNER_DETECTION_ORDER:
        if (root / marker).is_file():
            return runner_name, command
    return None, None


def _read_config(root: Path) -> dict | None:
    """Read the project config (``CONFIG_PATH_RELATIVE``) if well-formed (AC-8)."""
    path = root / CONFIG_PATH_RELATIVE
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"autofix: warning: could not parse {CONFIG_PATH_RELATIVE}: {exc}",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, dict):
        return None
    return data


def _resolve_test_command(
    root: Path,
) -> tuple[str | None, tuple[str, ...] | None, int, Path]:
    """Resolve (runner, command, timeout, cwd) from config or detection.

    Returns ``(None, None, timeout, cwd)`` when neither config nor any
    marker matches — the skip path (AC-10).
    """
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    cwd: Path = root

    config = _read_config(root)
    if config is not None:
        test_section = config.get("test")
        if isinstance(test_section, dict):
            cmd_raw = test_section.get("command")
            if isinstance(cmd_raw, list) and cmd_raw and all(
                isinstance(x, str) for x in cmd_raw
            ):
                command = tuple(cmd_raw)
                runner_name = test_section.get("runner") or "configured"
                t_raw = test_section.get("timeout_seconds")
                if isinstance(t_raw, int) and t_raw > 0:
                    timeout = t_raw
                cwd_raw = test_section.get("cwd")
                if isinstance(cwd_raw, str) and cwd_raw:
                    cwd = (root / cwd_raw).resolve()
                return runner_name, command, timeout, cwd

    runner_name, command = _detect_runner(root)
    return runner_name, command, timeout, cwd


def _run_test_command(
    *,
    command: tuple[str, ...],
    cwd: Path,
    timeout: int,
    log_path: Path,
) -> bool:
    """Run the test command. Returns ``test_passed: bool`` per AC-9."""
    try:
        result = subprocess.run(
            list(command),
            cwd=str(cwd),
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
        )
    except FileNotFoundError as exc:
        _safe_write_log(log_path, f"FileNotFoundError: {exc}\n")
        return False
    except subprocess.TimeoutExpired as exc:
        msg = (
            f"TimeoutExpired after {timeout}s\n"
            f"--- partial stdout ---\n{exc.stdout or ''}\n"
            f"--- partial stderr ---\n{exc.stderr or ''}\n"
        )
        _safe_write_log(log_path, msg)
        return False

    payload = (
        f"exit_code={result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}\n"
    )
    _safe_write_log(log_path, payload)
    return result.returncode == SUBPROCESS_EXIT_CODE_OK


def _safe_write_log(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def run_verification(
    *,
    root: Path,
    analyzer_set: list[str] | None,
    applied_finding_ids: frozenset[str],
    pre_finding_ids: frozenset[str],
    log_dir: Path,
    attempt: int,
    quiet: bool = False,
) -> VerifyResult:
    """Run tests + re-scan and return a :class:`VerifyResult` (AC-6).

    Raises :class:`VerifyScanFailed` if the post-apply re-scan exits
    non-zero (AC-15).
    """
    runner, command, timeout, cwd = _resolve_test_command(root)
    log_path = log_dir / VERIFY_LOG_FILENAME_TEMPLATE.format(attempt=attempt)

    if command is None:
        test_passed: bool | None = None
        test_log_path: Path | None = None
    else:
        test_passed = _run_test_command(
            command=command, cwd=cwd, timeout=timeout, log_path=log_path
        )
        test_log_path = log_path

    scan_result = _run_scan_core(
        root=root,
        full_sweep=True,
        analyzer_set=analyzer_set,
        scan_id=None,
        fresh_instance=False,
        quiet=quiet,
    )
    if scan_result.exit_code != SUBPROCESS_EXIT_CODE_OK:
        raise VerifyScanFailed(
            f"scan exited {scan_result.exit_code} during VERIFYING"
        )

    post_finding_ids = tuple(
        sorted(_finding_id(f) for f in scan_result.findings)
    )
    post_set = set(post_finding_ids)
    unresolved = frozenset(applied_finding_ids & post_set)
    new_finding_count = len(post_set - pre_finding_ids)

    regressed = (
        test_passed is False
        or new_finding_count > 0
        or len(unresolved) > 0
    )

    return VerifyResult(
        test_passed=test_passed,
        test_runner=runner,
        test_command=command,
        test_log_path=test_log_path,
        new_finding_count=new_finding_count,
        unresolved_finding_ids=unresolved,
        post_finding_ids=post_finding_ids,
        regressed=regressed,
    )


__all__ = ["VerifyResult", "VerifyScanFailed", "run_verification"]
