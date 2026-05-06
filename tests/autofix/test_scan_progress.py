"""Progress callback wiring for ``run_scan``.

The CLI passes a ``progress`` callable to surface stage-by-stage status
on stderr so a long scan does not look hung. Three contracts:

1. ``progress=None`` (default) is silent — no callbacks invoked, no
   exception, scan output unchanged from before this kwarg existed.
2. When a callable is passed, it is invoked at least once with a
   non-empty string for the stages we promise: planning invalidation,
   analyzing files, and the final SARIF/scheduler milestones.
3. Passing a non-callable does not crash mid-pipeline (the
   implementation only invokes ``progress`` when it is not ``None``;
   any other type would TypeError on first call — that is acceptable).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=root, check=True
    )


def _commit(root: Path, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", msg],
        cwd=root,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-05-06T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-05-06T00:00:00Z",
        },
    )


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    (tmp_path / "mod.py").write_text(
        "import json  # unused\n\nx = 1\n", encoding="utf-8"
    )
    _commit(tmp_path, "init")
    return tmp_path


def test_run_scan_progress_default_is_silent(tiny_repo: Path) -> None:
    """``run_scan`` with no ``progress`` kwarg invokes nothing — the
    library surface remains backwards compatible for embedded callers."""
    from autofix.events.schema import ChangeSet
    from autofix.funnel.pipeline import run_scan

    changeset = ChangeSet(paths=("mod.py",), watcher_confidence="diff-head1")
    # Smoke: would raise if progress was being invoked unconditionally.
    result = run_scan(tiny_repo, changeset, scan_id="scan_silent")
    assert result.scan_id == "scan_silent"


def test_run_scan_progress_invokes_callback(tiny_repo: Path) -> None:
    """When a callable is passed, ``run_scan`` emits at least one
    non-empty progress message during the analyze stage."""
    from autofix.events.schema import ChangeSet
    from autofix.funnel.pipeline import run_scan

    messages: list[str] = []

    def collect(msg: str) -> None:
        messages.append(msg)

    changeset = ChangeSet(paths=("mod.py",), watcher_confidence="diff-head1")
    run_scan(
        tiny_repo,
        changeset,
        scan_id="scan_progress",
        progress=collect,
    )

    assert messages, "expected at least one progress message"
    # Every emitted message must be a non-empty string.
    assert all(isinstance(m, str) and m for m in messages), messages
    # We promise an "Analyzing N file(s)" milestone — pin that
    # specifically so the contract is visible in tests.
    assert any(m.startswith("Analyzing ") for m in messages), messages


def test_run_scan_progress_does_not_change_findings(tiny_repo: Path) -> None:
    """Passing ``progress`` must not change scan output. The callback is
    pure observation; finding count, ids, and decisions stay identical."""
    from autofix.events.schema import ChangeSet
    from autofix.funnel.pipeline import run_scan

    changeset = ChangeSet(paths=("mod.py",), watcher_confidence="diff-head1")

    silent = run_scan(tiny_repo, changeset, scan_id="scan_a")
    noisy = run_scan(
        tiny_repo,
        changeset,
        scan_id="scan_b",
        progress=lambda _msg: None,
    )

    silent_ids = sorted(f.finding_id for f in silent.findings)
    noisy_ids = sorted(f.finding_id for f in noisy.findings)
    assert silent_ids == noisy_ids
