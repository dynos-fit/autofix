"""Tests for autofix.scan_all module.

Covers acceptance criterion: 20 (sequential scan, missing repo skip, exit code propagation).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from autofix.scan_all import cmd_scan_all, run_scan


def _make_git_repo(path: Path) -> Path:
    """Create a minimal git repo directory."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    (path / ".autofix").mkdir()
    return path


def _write_repos_json(home: Path, repos: list[dict]) -> None:
    autofix_dir = home / ".autofix"
    autofix_dir.mkdir(parents=True, exist_ok=True)
    (autofix_dir / "repos.json").write_text(json.dumps(repos))


# ---------------------------------------------------------------------------
# Criterion 20: sequential scan across all registered repos
# ---------------------------------------------------------------------------

class TestScanAll:
    """Criterion 20: scan-all iterates repos sequentially."""

    def test_scans_all_registered_repos(self, tmp_path: Path) -> None:
        repo_a = _make_git_repo(tmp_path / "repo-a")
        repo_b = _make_git_repo(tmp_path / "repo-b")
        home = tmp_path / "home"
        _write_repos_json(home, [
            {"path": str(repo_a)},
            {"path": str(repo_b)},
        ])

        scanned: list[str] = []

        def mock_scan(root, **kwargs):
            scanned.append(str(root))
            return 0  # success exit code

        with patch("autofix.scan_all.run_scan", side_effect=mock_scan):
            result = cmd_scan_all(home_dir=home)

        assert str(repo_a) in scanned
        assert str(repo_b) in scanned
        assert result.exit_code == 0

    def test_scans_repos_sequentially_in_order(self, tmp_path: Path) -> None:
        repos = []
        for name in ["repo-1", "repo-2", "repo-3"]:
            repos.append(_make_git_repo(tmp_path / name))
        home = tmp_path / "home"
        _write_repos_json(home, [{"path": str(r)} for r in repos])

        scan_order: list[str] = []

        def mock_scan(root, **kwargs):
            scan_order.append(str(root))
            return 0

        with patch("autofix.scan_all.run_scan", side_effect=mock_scan):
            cmd_scan_all(home_dir=home)

        assert scan_order == [str(r) for r in repos]

    def test_run_scan_uses_runtime_factory_and_repo_lock(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        runtime = object()

        with (
            patch("autofix.app.runtime_factory", return_value=runtime) as mock_runtime_factory,
            patch("autofix.scan_all.run_scan_with_lock", return_value=0) as mock_run_scan_with_lock,
        ):
            exit_code = run_scan(repo, max_findings=7)

        assert exit_code == 0
        mock_runtime_factory.assert_called_once_with(root=repo.resolve())
        mock_run_scan_with_lock.assert_called_once_with(repo.resolve(), max_findings=7, runtime=runtime)

    def test_run_scan_uses_repo_config_default_max_findings(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        runtime = object()
        (repo / ".autofix" / "config.json").write_text(json.dumps({"max_findings": 11}))

        with (
            patch("autofix.app.runtime_factory", return_value=runtime),
            patch("autofix.scan_all.run_scan_with_lock", return_value=0) as mock_run_scan_with_lock,
        ):
            exit_code = run_scan(repo)

        assert exit_code == 0
        mock_run_scan_with_lock.assert_called_once_with(repo.resolve(), max_findings=11, runtime=runtime)


# ---------------------------------------------------------------------------
# Criterion 20: missing repo skip with warning
# ---------------------------------------------------------------------------

class TestMissingRepoSkip:
    """Criterion 20: skip repos whose path no longer exists, print warning."""

    def test_skips_missing_repo_with_warning(self, tmp_path: Path) -> None:
        existing_repo = _make_git_repo(tmp_path / "existing")
        home = tmp_path / "home"
        _write_repos_json(home, [
            {"path": str(tmp_path / "nonexistent")},
            {"path": str(existing_repo)},
        ])

        scanned: list[str] = []

        def mock_scan(root, **kwargs):
            scanned.append(str(root))
            return 0

        with patch("autofix.scan_all.run_scan", side_effect=mock_scan):
            result = cmd_scan_all(home_dir=home)

        # Only the existing repo should have been scanned
        assert len(scanned) == 1
        assert str(existing_repo) in scanned
        # Warning about missing repo should be in the output
        assert "nonexistent" in result.output.lower() or "skip" in result.output.lower()

    def test_all_missing_repos_reports_all_skipped(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _write_repos_json(home, [
            {"path": str(tmp_path / "gone-a")},
            {"path": str(tmp_path / "gone-b")},
        ])

        with patch("autofix.scan_all.run_scan") as mock_scan:
            result = cmd_scan_all(home_dir=home)

        mock_scan.assert_not_called()
        assert result.exit_code == 0  # no failures, just skips


# ---------------------------------------------------------------------------
# Criterion 20 (implicit): exit code propagation
# ---------------------------------------------------------------------------

class TestExitCodePropagation:
    """Implicit requirement: non-zero exit if any repo scan fails."""

    def test_nonzero_exit_when_scan_fails(self, tmp_path: Path) -> None:
        repo_a = _make_git_repo(tmp_path / "repo-a")
        repo_b = _make_git_repo(tmp_path / "repo-b")
        home = tmp_path / "home"
        _write_repos_json(home, [
            {"path": str(repo_a)},
            {"path": str(repo_b)},
        ])

        def mock_scan(root, **kwargs):
            if "repo-a" in str(root):
                return 1  # failure
            return 0  # success

        with patch("autofix.scan_all.run_scan", side_effect=mock_scan):
            result = cmd_scan_all(home_dir=home)

        assert result.exit_code != 0

    def test_continues_scanning_after_failure(self, tmp_path: Path) -> None:
        """Even if one repo fails, remaining repos are still scanned."""
        repo_a = _make_git_repo(tmp_path / "repo-a")
        repo_b = _make_git_repo(tmp_path / "repo-b")
        home = tmp_path / "home"
        _write_repos_json(home, [
            {"path": str(repo_a)},
            {"path": str(repo_b)},
        ])

        scanned: list[str] = []

        def mock_scan(root, **kwargs):
            scanned.append(str(root))
            if "repo-a" in str(root):
                return 1
            return 0

        with patch("autofix.scan_all.run_scan", side_effect=mock_scan):
            cmd_scan_all(home_dir=home)

        assert len(scanned) == 2

    def test_zero_exit_when_all_succeed(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        home = tmp_path / "home"
        _write_repos_json(home, [{"path": str(repo)}])

        with patch("autofix.scan_all.run_scan", return_value=0):
            result = cmd_scan_all(home_dir=home)

        assert result.exit_code == 0

    def test_scan_exception_is_reported_in_output(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        home = tmp_path / "home"
        _write_repos_json(home, [{"path": str(repo)}])

        with patch("autofix.scan_all.run_scan", side_effect=ValueError("boom")):
            result = cmd_scan_all(home_dir=home)

        assert result.exit_code == 1
        assert "boom" in result.output

    def test_empty_repos_json(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _write_repos_json(home, [])

        result = cmd_scan_all(home_dir=home)
        assert result.exit_code == 0
