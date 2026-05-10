"""PROACTIVE-07: scan_core resets correlation contextvars on exception.

The proactive meta-audit flagged that no test pinned the
``run_scan``-path's exception-path contextvar reset. ``_run_scan_core``
binds ``_SCAN_ID`` and ``_COMMIT_SHA`` via a ``contextlib.ExitStack``;
the with-block's normal exit handles success, but the test suite did
not exercise an in-pipeline exception that propagates out of the
inner ``run_scan(...)`` call to confirm the reset still happens.

This is the SEC-RUFF-02 contextvar-leak class — same shape as the
crawler-cycle test added in PR #99
(``tests/autofix/cli/test_cycle_runner_correlation_contextvar_reset.py``)
but for the scan_command / scan_core code path.

Note: ``_run_scan_core`` catches ``Exception`` from ``run_scan`` and
returns a non-zero ``ScanCoreResult`` rather than propagating.
That's the success-of-the-cleanup we're verifying — even though the
inner call raised, the wrapping with-block still exited normally and
reset the tokens.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


def _seed_minimal_repo(tmp_path: Path) -> Path:
    """Tiny git repo with one Python file, one commit."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=tmp_path, check=True,
    )
    (tmp_path / "sample.py").write_text("import os\n")
    subprocess.run(["git", "add", "sample.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Boundary: parent-stamped contextvars survive a successful scan
# ---------------------------------------------------------------------------


def test_scan_core_does_not_leak_scan_id_on_success(tmp_path: Path) -> None:
    """A successful _run_scan_core call must restore the parent's _SCAN_ID."""
    from autofix.scan_core import _run_scan_core
    from autofix.telemetry.correlation import _SCAN_ID, current_scan_id

    repo = _seed_minimal_repo(tmp_path)
    sentinel = "PARENT_SCAN_ID_BEFORE_SUCCESS"
    token = _SCAN_ID.set(sentinel)
    try:
        result = _run_scan_core(
            root=repo,
            full_sweep=True,
            analyzer_set=None,
            quiet=True,
        )
        # Either exit 0 or 1 is fine for this test — what matters is the
        # contextvar boundary held.
        assert result.exit_code in (0, 1), result
        assert current_scan_id() == sentinel, (
            f"scan_core leaked scan_id on success: got {current_scan_id()!r}"
        )
    finally:
        _SCAN_ID.reset(token)


def test_scan_core_does_not_leak_commit_sha_on_success(tmp_path: Path) -> None:
    """A successful _run_scan_core call must restore the parent's _COMMIT_SHA."""
    from autofix.scan_core import _run_scan_core
    from autofix.telemetry.correlation import _COMMIT_SHA, current_commit_sha

    repo = _seed_minimal_repo(tmp_path)
    sentinel = "PARENT_COMMIT_SHA_BEFORE_SUCCESS"
    token = _COMMIT_SHA.set(sentinel)
    try:
        result = _run_scan_core(
            root=repo,
            full_sweep=True,
            analyzer_set=None,
            quiet=True,
        )
        assert result.exit_code in (0, 1), result
        assert current_commit_sha() == sentinel, (
            f"scan_core leaked commit_sha on success: got {current_commit_sha()!r}"
        )
    finally:
        _COMMIT_SHA.reset(token)


# ---------------------------------------------------------------------------
# Boundary: parent-stamped contextvars survive an in-pipeline exception
# (the SEC-RUFF-02 lesson — the missing test the audit flagged)
# ---------------------------------------------------------------------------


def test_scan_core_does_not_leak_scan_id_on_run_scan_exception(
    tmp_path: Path,
) -> None:
    """When run_scan raises, the outer with-block's reset still runs.

    _run_scan_core catches the exception and returns exit_code=1 — but
    the contract here is that ``_SCAN_ID.reset(token)`` fires regardless,
    via the ExitStack's normal exit. A future regression that moved the
    setter outside the with-block would break this test.
    """
    from autofix.scan_core import _run_scan_core
    from autofix.telemetry.correlation import _SCAN_ID, current_scan_id

    repo = _seed_minimal_repo(tmp_path)
    sentinel = "PARENT_SCAN_ID_BEFORE_RAISE"
    token = _SCAN_ID.set(sentinel)
    try:
        with patch(
            "autofix.scan_core.run_scan",
            side_effect=RuntimeError("forced funnel failure"),
        ):
            result = _run_scan_core(
                root=repo,
                full_sweep=True,
                analyzer_set=None,
                quiet=True,
            )
            # _run_scan_core catches and returns exit_code=1
            assert result.exit_code == 1, result
        # Contextvar must still be back to the sentinel.
        assert current_scan_id() == sentinel, (
            f"scan_core leaked scan_id on exception path: "
            f"got {current_scan_id()!r}"
        )
    finally:
        _SCAN_ID.reset(token)


def test_scan_core_does_not_leak_commit_sha_on_run_scan_exception(
    tmp_path: Path,
) -> None:
    """Symmetric to above — _COMMIT_SHA also resets when run_scan raises."""
    from autofix.scan_core import _run_scan_core
    from autofix.telemetry.correlation import _COMMIT_SHA, current_commit_sha

    repo = _seed_minimal_repo(tmp_path)
    sentinel = "PARENT_COMMIT_SHA_BEFORE_RAISE"
    token = _COMMIT_SHA.set(sentinel)
    try:
        with patch(
            "autofix.scan_core.run_scan",
            side_effect=RuntimeError("forced funnel failure"),
        ):
            result = _run_scan_core(
                root=repo,
                full_sweep=True,
                analyzer_set=None,
                quiet=True,
            )
            assert result.exit_code == 1, result
        assert current_commit_sha() == sentinel, (
            f"scan_core leaked commit_sha on exception path: "
            f"got {current_commit_sha()!r}"
        )
    finally:
        _COMMIT_SHA.reset(token)
