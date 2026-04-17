"""Tests for autofix_next.events.change_detector.

Covers:
  AC #5 — default diff range HEAD~1..HEAD, --full-sweep iterates git ls-files.
  AC #6 — single-commit repo → full-sweep-fallback with
          watcher_confidence == "full-sweep-fallback".
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)


def _commit(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", message],
        cwd=root,
        check=True,
        env={**os.environ, "GIT_AUTHOR_DATE": "2026-04-17T00:00:00Z",
             "GIT_COMMITTER_DATE": "2026-04-17T00:00:00Z"},
    )


@pytest.fixture
def two_commit_repo(tmp_path: Path) -> Path:
    """Repo with two commits; second commit touches `changed.py`, first adds
    only `unchanged.py`."""
    _init_git_repo(tmp_path)
    (tmp_path / "unchanged.py").write_text("x = 1\n")
    _commit(tmp_path, "initial")
    (tmp_path / "changed.py").write_text("y = 2\n")
    _commit(tmp_path, "second")
    return tmp_path


@pytest.fixture
def single_commit_repo(tmp_path: Path) -> Path:
    """Repo with exactly one commit — HEAD~1 does not exist."""
    _init_git_repo(tmp_path)
    (tmp_path / "only.py").write_text("z = 3\n")
    (tmp_path / "notes.txt").write_text("ignore me\n")
    _commit(tmp_path, "only commit")
    return tmp_path


def test_default_diff_range(two_commit_repo: Path) -> None:
    """AC #5: default range is HEAD~1..HEAD, filtered to *.py."""
    from autofix_next.events.change_detector import detect

    changeset, watcher_confidence = detect(two_commit_repo, full_sweep=False)

    # Default diff of HEAD~1..HEAD: only `changed.py` was added in HEAD.
    paths = [str(p) for p in changeset.paths]
    assert any("changed.py" in p for p in paths), paths
    assert not any("unchanged.py" in p for p in paths), paths
    assert watcher_confidence == "diff-head1"


def test_full_sweep_flag(two_commit_repo: Path) -> None:
    """AC #5: --full-sweep returns every *.py from `git ls-files`."""
    from autofix_next.events.change_detector import detect

    changeset, watcher_confidence = detect(two_commit_repo, full_sweep=True)

    paths = [str(p) for p in changeset.paths]
    assert any("changed.py" in p for p in paths), paths
    assert any("unchanged.py" in p for p in paths), paths
    assert watcher_confidence == "full-sweep"


def test_single_commit_fallback_sets_watcher_confidence(
    single_commit_repo: Path,
) -> None:
    """AC #6: single-commit repo falls back to full sweep and watcher_confidence
    is the literal string 'full-sweep-fallback'."""
    from autofix_next.events.change_detector import detect

    changeset, watcher_confidence = detect(single_commit_repo, full_sweep=False)

    assert watcher_confidence == "full-sweep-fallback"
    paths = [str(p) for p in changeset.paths]
    assert any("only.py" in p for p in paths), paths
    # Non-.py files are not in the changeset
    assert not any("notes.txt" in p for p in paths), paths
