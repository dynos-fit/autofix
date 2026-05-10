"""Per-cycle correlation contextvar reset (PROACTIVE-11).

Long-running daemons (``run_crawl_continuously``) call
``_run_crawl_once_body`` once per cycle. Before this fix, the body
did NOT bind a fresh ``scan_id`` / ``commit_sha`` contextvar at the
cycle boundary — meaning whatever values were set in the parent
process leaked across every cycle, AND any failure mid-cycle could
leave a stale id visible to the next cycle's downstream readers
(LLM cache keys, telemetry events, finding fingerprints).

This is the SEC-RUFF-02 contextvar-leak class generalized to the
daemon outer loop. The fix wraps the body in a ``contextlib.ExitStack``
that enters ``set_scan_id`` (and ``set_commit_sha`` when non-empty)
for the cycle's duration; both reset on success AND exception via
the underlying ``@contextmanager`` decorators in
``autofix.telemetry.correlation``.

These tests pin the boundary contract: contextvars set by the
parent are observed AFTER the cycle, not before — proving the
inner cycle's stamping doesn't leak out.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from autofix.cli.cycle_runner import _mint_cycle_scan_id, run_crawl_once
from autofix.telemetry.correlation import (
    _COMMIT_SHA,
    _SCAN_ID,
    current_commit_sha,
    current_scan_id,
)


def _seed_minimal_repo(tmp_path: Path) -> Path:
    """Tiny git repo with one Python file, one commit."""
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=tmp_path, check=True,
    )
    (tmp_path / "sample.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "sample.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=tmp_path, check=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------


def test_mint_cycle_scan_id_format() -> None:
    """``crawl-<UTCstamp>-<8hex>`` — parseable, prefix distinguishes from one-shot scans."""
    sid = _mint_cycle_scan_id()
    assert sid.startswith("crawl-"), f"expected crawl- prefix: {sid!r}"
    parts = sid.split("-")
    assert len(parts) == 3, f"expected 3 hyphen-separated parts: {sid!r}"
    assert len(parts[2]) == 8, f"expected 8-char hex suffix: {sid!r}"
    int(parts[2], 16)  # raises if not valid hex


def test_mint_cycle_scan_id_unique() -> None:
    """Two consecutive calls produce different ids."""
    a = _mint_cycle_scan_id()
    b = _mint_cycle_scan_id()
    assert a != b


# ---------------------------------------------------------------------------
# Boundary: parent-stamped contextvars survive a successful cycle
# ---------------------------------------------------------------------------


def test_cycle_does_not_leak_scan_id_to_parent_on_success(tmp_path: Path) -> None:
    """Parent's scan_id sentinel is preserved across a successful cycle."""
    repo = _seed_minimal_repo(tmp_path)
    sentinel = "PARENT_SCAN_ID_SENTINEL"
    token = _SCAN_ID.set(sentinel)
    try:
        rc = run_crawl_once(
            root=repo, mode="preview", budget="cheap", quiet=True,
        )
        assert rc == 0
        assert current_scan_id() == sentinel, (
            f"cycle leaked its scan_id to parent: got {current_scan_id()!r}"
        )
    finally:
        _SCAN_ID.reset(token)


def test_cycle_does_not_leak_commit_sha_to_parent_on_success(tmp_path: Path) -> None:
    """Parent's commit_sha sentinel is preserved across a successful cycle."""
    repo = _seed_minimal_repo(tmp_path)
    sentinel = "PARENT_COMMIT_SHA_SENTINEL"
    token = _COMMIT_SHA.set(sentinel)
    try:
        rc = run_crawl_once(
            root=repo, mode="preview", budget="cheap", quiet=True,
        )
        assert rc == 0
        assert current_commit_sha() == sentinel, (
            f"cycle leaked its commit_sha to parent: got {current_commit_sha()!r}"
        )
    finally:
        _COMMIT_SHA.reset(token)


# ---------------------------------------------------------------------------
# Boundary: parent-stamped contextvars survive a FAILING cycle
# (the SEC-RUFF-02 lesson — exception path correctness)
# ---------------------------------------------------------------------------


def test_cycle_does_not_leak_scan_id_to_parent_on_exception(
    tmp_path: Path,
) -> None:
    """When the cycle body raises, the contextvar still resets at the boundary.

    Patches ``pick_next_batch`` to raise — the outer ``with set_scan_id(...)``
    must reset the contextvar in its ``finally`` regardless of the inner
    exception. This is the SEC-RUFF-02 incident class generalized.
    """
    repo = _seed_minimal_repo(tmp_path)
    sentinel = "PARENT_SCAN_ID_BEFORE_RAISE"
    token = _SCAN_ID.set(sentinel)
    try:
        with patch(
            "autofix.crawl.picker.pick_next_batch",
            side_effect=RuntimeError("forced cycle failure"),
        ):
            with pytest.raises(RuntimeError, match="forced cycle failure"):
                run_crawl_once(
                    root=repo, mode="preview", budget="cheap", quiet=True,
                )
        # Reset must have run despite the exception propagating.
        assert current_scan_id() == sentinel, (
            f"cycle leaked scan_id on exception path: got {current_scan_id()!r}"
        )
    finally:
        _SCAN_ID.reset(token)


def test_cycle_does_not_leak_commit_sha_to_parent_on_exception(
    tmp_path: Path,
) -> None:
    """Same as above but for commit_sha — both contextvars must reset on raise."""
    repo = _seed_minimal_repo(tmp_path)
    sentinel = "PARENT_COMMIT_SHA_BEFORE_RAISE"
    token = _COMMIT_SHA.set(sentinel)
    try:
        with patch(
            "autofix.crawl.picker.pick_next_batch",
            side_effect=RuntimeError("forced cycle failure"),
        ):
            with pytest.raises(RuntimeError, match="forced cycle failure"):
                run_crawl_once(
                    root=repo, mode="preview", budget="cheap", quiet=True,
                )
        assert current_commit_sha() == sentinel, (
            f"cycle leaked commit_sha on exception path: "
            f"got {current_commit_sha()!r}"
        )
    finally:
        _COMMIT_SHA.reset(token)
