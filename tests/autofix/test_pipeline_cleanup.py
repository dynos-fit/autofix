"""Test pipeline cleanup decorator (AC-11).

Verify the @_with_per_scan_cleanup decorator's finally clause clears
ALL THREE adapters' memos on both success and exception paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest


def _init_repo(root: Path) -> None:
    """Initialize a git repository with standard config."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=root, check=True
    )


def _commit(root: Path, msg: str) -> None:
    """Commit all changes to git."""
    import os

    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", msg],
        cwd=root,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-04-17T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-04-17T00:00:00Z",
        },
    )


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    """Create a tiny git repo with a Python file."""
    _init_repo(tmp_path)
    (tmp_path / "module.py").write_text("x = 1\n", encoding="utf-8")
    _commit(tmp_path, "init")
    return tmp_path


def test_decorator_clears_all_three_adapters_on_success(tiny_repo: Path) -> None:
    """@_with_per_scan_cleanup clears ruff, mypy, and llm_judgment memos on success."""
    from autofix.analyzers.linter_passthrough import ruff, mypy
    from autofix.analyzers.llm_judgment._base import (
        LLMJudgmentAnalyzer,
        _PER_SCAN_EVENTS,
    )
    from autofix.events.schema import ChangeSet
    from autofix.funnel.pipeline import run_scan

    # Manually populate the state dicts to simulate previous errors
    ruff._PER_SCAN_EVENTS["test-scan"] = {"AnalyzerUnavailable"}
    mypy._PER_SCAN_EVENTS["test-scan"] = {"AnalyzerError"}
    _PER_SCAN_EVENTS["test-scan"] = {"AnalyzerUnavailable"}

    # Verify populated
    assert len(ruff._PER_SCAN_EVENTS) > 0
    assert len(mypy._PER_SCAN_EVENTS) > 0
    assert len(_PER_SCAN_EVENTS) > 0

    # Run scan (which applies @_with_per_scan_cleanup)
    changeset = ChangeSet(paths=("module.py",), watcher_confidence="diff-head1")
    result = run_scan(tiny_repo, changeset, scan_id="test-scan-cleanup-1")

    # Verify all three are cleared
    assert len(ruff._PER_SCAN_EVENTS) == 0
    assert len(mypy._PER_SCAN_EVENTS) == 0
    assert len(_PER_SCAN_EVENTS) == 0


def test_decorator_clears_all_three_adapters_on_exception(
    tiny_repo: Path, monkeypatch
) -> None:
    """@_with_per_scan_cleanup clears memos even when run_scan raises an exception."""
    from autofix.analyzers.linter_passthrough import ruff, mypy
    from autofix.analyzers.llm_judgment._base import (
        LLMJudgmentAnalyzer,
        _PER_SCAN_EVENTS,
    )
    from autofix.events.schema import ChangeSet
    from autofix.funnel.pipeline import run_scan

    # Manually populate the state dicts
    ruff._PER_SCAN_EVENTS["test-scan"] = {"AnalyzerUnavailable"}
    mypy._PER_SCAN_EVENTS["test-scan"] = {"AnalyzerError"}
    _PER_SCAN_EVENTS["test-scan"] = {"AnalyzerUnavailable"}

    # Inject an exception deep inside run_scan
    # Monkeypatch the CallGraph.build_from_root to raise an error
    monkeypatch.setattr(
        "autofix.invalidation.call_graph.CallGraph.build_from_root",
        classmethod(lambda *a, **kw: 1 / 0),  # ZeroDivisionError
    )

    changeset = ChangeSet(paths=("module.py",), watcher_confidence="diff-head1")

    # Expect the exception to be raised
    with pytest.raises(ZeroDivisionError):
        run_scan(tiny_repo, changeset, scan_id="test-scan-cleanup-2")

    # But memos should still be cleared
    assert len(ruff._PER_SCAN_EVENTS) == 0
    assert len(mypy._PER_SCAN_EVENTS) == 0
    assert len(_PER_SCAN_EVENTS) == 0
