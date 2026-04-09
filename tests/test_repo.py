"""Tests for autofix.repo module.

Covers acceptance criteria: 14, 15.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autofix.repo import repo_add, repo_list, repo_remove


def _make_git_repo(path: Path) -> Path:
    """Create a minimal directory with .git/ to simulate a git repo."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def _read_repos_json(home: Path) -> list[dict]:
    repos_file = home / ".autofix" / "repos.json"
    if not repos_file.exists():
        return []
    return json.loads(repos_file.read_text())


# ---------------------------------------------------------------------------
# Criterion 14: add / remove / list
# ---------------------------------------------------------------------------

class TestRepoAdd:
    """Criterion 14: autofix repo add registers a path in repos.json."""

    def test_add_registers_repo(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "my-repo")
        home = tmp_path / "home"
        result = repo_add(path=repo, home_dir=home)
        assert result.exit_code == 0
        repos = _read_repos_json(home)
        paths = [e["path"] for e in repos]
        assert str(repo.resolve()) in paths

    def test_add_deduplicates(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "my-repo")
        home = tmp_path / "home"
        repo_add(path=repo, home_dir=home)
        repo_add(path=repo, home_dir=home)
        repos = _read_repos_json(home)
        paths = [e["path"] for e in repos]
        assert paths.count(str(repo.resolve())) == 1

    def test_add_multiple_repos(self, tmp_path: Path) -> None:
        repo_a = _make_git_repo(tmp_path / "repo-a")
        repo_b = _make_git_repo(tmp_path / "repo-b")
        home = tmp_path / "home"
        repo_add(path=repo_a, home_dir=home)
        repo_add(path=repo_b, home_dir=home)
        repos = _read_repos_json(home)
        paths = [e["path"] for e in repos]
        assert str(repo_a.resolve()) in paths
        assert str(repo_b.resolve()) in paths


class TestRepoRemove:
    """Criterion 14: autofix repo remove removes a path from repos.json."""

    def test_remove_existing_repo(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "my-repo")
        home = tmp_path / "home"
        repo_add(path=repo, home_dir=home)
        result = repo_remove(path=repo, home_dir=home)
        assert result.exit_code == 0
        repos = _read_repos_json(home)
        paths = [e["path"] for e in repos]
        assert str(repo.resolve()) not in paths

    def test_remove_nonexistent_repo_exits_0(self, tmp_path: Path) -> None:
        """Removing a repo that is not registered should not fail hard."""
        home = tmp_path / "home"
        (home / ".autofix").mkdir(parents=True)
        (home / ".autofix" / "repos.json").write_text("[]")
        result = repo_remove(path=tmp_path / "nonexistent", home_dir=home)
        # Should not crash -- may exit 0 or 1 depending on design, but not crash
        assert result.exit_code in (0, 1)


class TestRepoList:
    """Criterion 14: autofix repo list prints all registered repos."""

    def test_list_returns_registered_repos(self, tmp_path: Path, capsys) -> None:
        repo_a = _make_git_repo(tmp_path / "repo-a")
        repo_b = _make_git_repo(tmp_path / "repo-b")
        home = tmp_path / "home"
        repo_add(path=repo_a, home_dir=home)
        repo_add(path=repo_b, home_dir=home)
        result = repo_list(home_dir=home)
        assert result.exit_code == 0
        # Result should contain the repo paths
        assert str(repo_a.resolve()) in result.output
        assert str(repo_b.resolve()) in result.output

    def test_list_empty_repos(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        (home / ".autofix").mkdir(parents=True)
        (home / ".autofix" / "repos.json").write_text("[]")
        result = repo_list(home_dir=home)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Criterion 15: .git validation
# ---------------------------------------------------------------------------

class TestGitValidation:
    """Criterion 15: repo add validates .git/ directory exists."""

    def test_rejects_non_git_directory(self, tmp_path: Path) -> None:
        non_git_dir = tmp_path / "plain-dir"
        non_git_dir.mkdir()
        home = tmp_path / "home"
        result = repo_add(path=non_git_dir, home_dir=home)
        assert result.exit_code == 1
        assert "git" in result.message.lower() or ".git" in result.message.lower()

    def test_rejects_nonexistent_path(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        result = repo_add(path=tmp_path / "does-not-exist", home_dir=home)
        assert result.exit_code == 1

    def test_rejects_file_instead_of_directory(self, tmp_path: Path) -> None:
        a_file = tmp_path / "afile.txt"
        a_file.write_text("not a directory")
        home = tmp_path / "home"
        result = repo_add(path=a_file, home_dir=home)
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Implicit requirement: repos.json initialization
# ---------------------------------------------------------------------------

class TestReposJsonInit:
    """Implicit: repos.json and ~/.autofix/ are created if missing."""

    def test_creates_autofix_dir_and_repos_json(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        home = tmp_path / "fresh-home"
        assert not (home / ".autofix").exists()
        repo_add(path=repo, home_dir=home)
        assert (home / ".autofix" / "repos.json").exists()
        repos = _read_repos_json(home)
        assert len(repos) == 1
