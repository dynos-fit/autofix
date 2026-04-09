"""Tests for autofix.init module.

Covers acceptance criteria: 4, 5, 6, 16.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from autofix.init import cmd_init


def _make_git_repo(path: Path) -> Path:
    """Create a minimal directory that looks like a git repo."""
    (path / ".git").mkdir(parents=True)
    return path


def _read_repos_json(home: Path) -> list[dict]:
    repos_file = home / ".autofix" / "repos.json"
    if not repos_file.exists():
        return []
    return json.loads(repos_file.read_text())


# ---------------------------------------------------------------------------
# Criterion 5: prerequisite checks (git, gh, claude)
# ---------------------------------------------------------------------------

class TestPrerequisiteChecks:
    """Criterion 5: autofix init checks for git, gh, and claude on PATH."""

    def test_missing_all_tools_reports_all_and_exits_1(self, tmp_path: Path) -> None:
        """When all three tools are missing, error names all of them."""
        repo = _make_git_repo(tmp_path / "repo")
        with patch("autofix.init.shutil.which", return_value=None):
            result = cmd_init(root=repo, home_dir=tmp_path / "home")
        assert result.exit_code == 1
        assert "git" in result.message
        assert "gh" in result.message
        assert "claude" in result.message

    def test_missing_one_tool_reports_it_and_exits_1(self, tmp_path: Path) -> None:
        """When only 'claude' is missing, error names it specifically."""
        repo = _make_git_repo(tmp_path / "repo")

        def selective_which(name: str) -> str | None:
            return None if name == "claude" else f"/usr/bin/{name}"

        with patch("autofix.init.shutil.which", side_effect=selective_which):
            result = cmd_init(root=repo, home_dir=tmp_path / "home")
        assert result.exit_code == 1
        assert "claude" in result.message
        # Tools that ARE present should not be listed as missing.
        assert "git" not in result.message or "missing" not in result.message.lower()

    def test_no_files_created_when_prerequisites_missing(self, tmp_path: Path) -> None:
        """No .autofix/ directory or repos.json created when tools are missing."""
        repo = _make_git_repo(tmp_path / "repo")
        home = tmp_path / "home"
        with patch("autofix.init.shutil.which", return_value=None):
            cmd_init(root=repo, home_dir=home)
        assert not (repo / ".autofix").exists()
        assert not (home / ".autofix" / "repos.json").exists()

    def test_all_tools_present_succeeds(self, tmp_path: Path) -> None:
        """When all tools are on PATH, init succeeds."""
        repo = _make_git_repo(tmp_path / "repo")
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            result = cmd_init(root=repo, home_dir=tmp_path / "home")
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Criterion 4: .autofix/ creation, policy file, repos.json registration
# ---------------------------------------------------------------------------

class TestAutofixDirCreation:
    """Criterion 4: autofix init creates .autofix/, writes default policy, registers repo."""

    def test_creates_autofix_directory(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            cmd_init(root=repo, home_dir=tmp_path / "home")
        assert (repo / ".autofix").is_dir()

    def test_writes_default_policy_file(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            cmd_init(root=repo, home_dir=tmp_path / "home")
        policy_file = repo / ".autofix" / "autofix-policy.json"
        assert policy_file.exists()
        policy = json.loads(policy_file.read_text())
        # Policy should be a non-empty dict (from default_category_policy)
        assert isinstance(policy, dict)
        assert len(policy) > 0

    def test_registers_repo_in_repos_json(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        home = tmp_path / "home"
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            cmd_init(root=repo, home_dir=home)
        repos = _read_repos_json(home)
        paths = [entry["path"] for entry in repos]
        assert str(repo.resolve()) in paths

    def test_creates_home_autofix_dir_if_missing(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        home = tmp_path / "home"
        assert not (home / ".autofix").exists()
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            cmd_init(root=repo, home_dir=home)
        assert (home / ".autofix").is_dir()
        assert (home / ".autofix" / "repos.json").exists()


# ---------------------------------------------------------------------------
# Criterion 6: idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Criterion 6: Running init twice does not duplicate entries or overwrite policy."""

    def test_no_duplicate_repo_entry(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        home = tmp_path / "home"
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            cmd_init(root=repo, home_dir=home)
            cmd_init(root=repo, home_dir=home)
        repos = _read_repos_json(home)
        paths = [entry["path"] for entry in repos]
        assert paths.count(str(repo.resolve())) == 1

    def test_does_not_overwrite_existing_policy(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        home = tmp_path / "home"
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            cmd_init(root=repo, home_dir=home)
        # Modify the policy file manually
        policy_file = repo / ".autofix" / "autofix-policy.json"
        custom_policy = {"custom": "policy"}
        policy_file.write_text(json.dumps(custom_policy))
        # Run init again
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            result = cmd_init(root=repo, home_dir=home)
        assert result.exit_code == 0
        # Policy should not have been overwritten
        assert json.loads(policy_file.read_text()) == custom_policy

    def test_second_run_exits_successfully(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        home = tmp_path / "home"
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            r1 = cmd_init(root=repo, home_dir=home)
            r2 = cmd_init(root=repo, home_dir=home)
        assert r1.exit_code == 0
        assert r2.exit_code == 0


# ---------------------------------------------------------------------------
# Criterion 16: config overrides via --max-files and --interval
# ---------------------------------------------------------------------------

class TestConfigOverrides:
    """Criterion 16: autofix init --max-files/--interval writes per-repo config."""

    def test_max_files_written_to_config_json(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            cmd_init(root=repo, home_dir=tmp_path / "home", max_files=12)
        config = json.loads((repo / ".autofix" / "config.json").read_text())
        assert config["max_files"] == 12

    def test_interval_written_to_config_json(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            cmd_init(root=repo, home_dir=tmp_path / "home", interval="1h")
        config = json.loads((repo / ".autofix" / "config.json").read_text())
        assert config["interval"] == "1h"

    def test_no_config_json_when_no_overrides(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            cmd_init(root=repo, home_dir=tmp_path / "home")
        # config.json should not exist if no overrides were provided
        assert not (repo / ".autofix" / "config.json").exists()

    def test_reinit_with_different_flags_updates_without_resetting_policy(
        self, tmp_path: Path
    ) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        home = tmp_path / "home"
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            cmd_init(root=repo, home_dir=home, max_files=8)
        # Modify policy manually
        policy_file = repo / ".autofix" / "autofix-policy.json"
        custom = {"my": "policy"}
        policy_file.write_text(json.dumps(custom))
        # Re-init with different flags
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            cmd_init(root=repo, home_dir=home, interval="15m")
        # Config should have both old and new values merged
        config = json.loads((repo / ".autofix" / "config.json").read_text())
        assert config["max_files"] == 8
        assert config["interval"] == "15m"
        # Policy should not have been reset
        assert json.loads(policy_file.read_text()) == custom

    def test_reinit_overwrites_same_config_key(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path / "repo")
        home = tmp_path / "home"
        with patch("autofix.init.shutil.which", return_value="/usr/bin/tool"):
            cmd_init(root=repo, home_dir=home, max_files=8)
            cmd_init(root=repo, home_dir=home, max_files=12)
        config = json.loads((repo / ".autofix" / "config.json").read_text())
        assert config["max_files"] == 12
