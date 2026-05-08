"""apply_post_fix_policy behavior tests (ARCH-015, AC-4..9, AC-11..12, AC-19..26)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autofix.cli.post_fix_constants import (
    POST_FIX_BRANCH,
    POST_FIX_BRANCH_PR,
    POST_FIX_WORKING_TREE,
)
from autofix.cli import post_fix_policy as policy_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    """Init a tmp git repo with one initial commit + one staged change."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.invalid"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=repo, check=True,
    )
    (repo / "README.md").write_text("# repo\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True
    )
    # An applied fix lives in the working tree as an unstaged modification.
    (repo / "fixed.py").write_text("def foo(): pass\n")


def _current_branch(repo: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo, check=True, text=True, capture_output=True,
    )
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# Behavior tests
# ---------------------------------------------------------------------------


def test_working_tree_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-5 + AC-19: policy=working-tree must perform zero git operations."""
    sentinel = MagicMock()
    monkeypatch.setattr(policy_mod, "_run_git", sentinel)

    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-1",
        applied_finding_ids=frozenset({"F-1"}),
        policy=POST_FIX_WORKING_TREE,
        quiet=True,
    )
    assert rc == POST_FIX_WORKING_TREE
    sentinel.assert_not_called()


def test_branch_creates_branch_and_commits(tmp_path: Path) -> None:
    """AC-6 + AC-20: policy=branch creates branch, commits, restores."""
    _git_init(tmp_path)
    initial_branch = _current_branch(tmp_path)

    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-abc",
        applied_finding_ids=frozenset({"F-1", "F-2"}),
        policy=POST_FIX_BRANCH,
        quiet=True,
    )
    assert rc == POST_FIX_BRANCH

    # Original branch restored.
    assert _current_branch(tmp_path) == initial_branch

    # New branch exists with exactly one new commit.
    branch_log = subprocess.run(
        ["git", "log", "--oneline", "autofix/fixes-r-abc",
         f"^{initial_branch}"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    new_commits = [l for l in branch_log.stdout.splitlines() if l.strip()]
    assert len(new_commits) == 1


def test_commit_message_structure(tmp_path: Path) -> None:
    """AC-21: commit message has the documented title + body."""
    _git_init(tmp_path)

    policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-msg",
        applied_finding_ids=frozenset({"F-2", "F-1", "F-3"}),
        policy=POST_FIX_BRANCH,
        quiet=True,
    )

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%B", "autofix/fixes-r-msg"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    msg = log.stdout
    assert "autofix: applied 3 fixes (run r-msg)" in msg
    # Bullets sorted ascending.
    assert msg.find("- finding-id: F-1") < msg.find("- finding-id: F-2") < msg.find("- finding-id: F-3")


def test_branch_pr_without_gh_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-8 + AC-22: gh absent → fall back to branch."""
    _git_init(tmp_path)
    monkeypatch.setattr(policy_mod.shutil, "which", lambda _: None)

    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-no-gh",
        applied_finding_ids=frozenset({"F-1"}),
        policy=POST_FIX_BRANCH_PR,
        quiet=True,
    )
    assert rc == POST_FIX_BRANCH


def test_branch_pr_with_gh_invokes_pr_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7 + AC-23: gh present → invoke gh pr create with the documented args."""
    _git_init(tmp_path)
    monkeypatch.setattr(policy_mod.shutil, "which", lambda _: "/usr/bin/gh")

    captured = {}

    def _fake_gh(root, args):
        captured["args"] = tuple(args)
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(policy_mod, "_run_gh", _fake_gh)

    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-pr",
        applied_finding_ids=frozenset({"F-1"}),
        policy=POST_FIX_BRANCH_PR,
        quiet=True,
    )
    assert rc == POST_FIX_BRANCH_PR
    assert captured["args"][:4] == ("gh", "pr", "create", "--fill")
    assert "--head" in captured["args"]
    assert "autofix/fixes-r-pr" in captured["args"]


def test_subprocess_failure_degrades_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-9 + AC-24: subprocess error → revert to working-tree, restore branch, no raise."""
    _git_init(tmp_path)

    real_run_git = policy_mod._run_git

    def _flaky_git(root, args):
        # Fail on the first commit attempt only.
        if args[:1] == ["commit"]:
            raise subprocess.CalledProcessError(
                returncode=1, cmd=["git", *args],
                stderr="commit failed",
            )
        return real_run_git(root, args)

    monkeypatch.setattr(policy_mod, "_run_git", _flaky_git)

    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-fail",
        applied_finding_ids=frozenset({"F-1"}),
        policy=POST_FIX_BRANCH,
        quiet=False,
    )
    assert rc == POST_FIX_WORKING_TREE
    err = capsys.readouterr().err
    assert "post-fix policy branch failed" in err


def test_resolution_cli_beats_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-11 + AC-25: CLI override beats config."""
    cfg = tmp_path / ".autofix"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"post_fix": "branch"}))

    sentinel = MagicMock()
    monkeypatch.setattr(policy_mod, "_run_git", sentinel)

    # CLI says working-tree; config says branch. CLI wins → no-op.
    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-1",
        applied_finding_ids=frozenset({"F-1"}),
        policy=POST_FIX_WORKING_TREE,
        quiet=True,
    )
    assert rc == POST_FIX_WORKING_TREE
    sentinel.assert_not_called()


def test_resolution_config_beats_default(tmp_path: Path) -> None:
    """AC-25: config beats default when CLI is None."""
    _git_init(tmp_path)
    cfg = tmp_path / ".autofix"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"post_fix": "branch"}))

    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-cfg",
        applied_finding_ids=frozenset({"F-1"}),
        policy=None,
        quiet=True,
    )
    assert rc == POST_FIX_BRANCH


def test_unknown_config_value_falls_back_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-12: unknown config value → default + stderr warning."""
    cfg = tmp_path / ".autofix"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"post_fix": "garbage"}))

    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-bad",
        applied_finding_ids=frozenset({"F-1"}),
        policy=None,
        quiet=False,
    )
    assert rc == POST_FIX_WORKING_TREE
    err = capsys.readouterr().err
    assert "unknown post_fix policy" in err


def test_malformed_config_falls_back_silently(tmp_path: Path) -> None:
    """AC-12: malformed JSON → treated as absent → default."""
    cfg = tmp_path / ".autofix"
    cfg.mkdir()
    (cfg / "config.json").write_text("{not valid json")

    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-mal",
        applied_finding_ids=frozenset({"F-1"}),
        policy=None,
        quiet=True,
    )
    assert rc == POST_FIX_WORKING_TREE


def test_empty_applied_set_skips_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-26: empty applied_finding_ids → no-op even with policy=branch."""
    sentinel = MagicMock()
    monkeypatch.setattr(policy_mod, "_run_git", sentinel)

    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-empty",
        applied_finding_ids=frozenset(),
        policy=POST_FIX_BRANCH,
        quiet=True,
    )
    assert rc == POST_FIX_WORKING_TREE
    sentinel.assert_not_called()


def test_unknown_cli_override_falls_back_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An out-of-allowed CLI override falls back to default with a warning."""
    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-bad-cli",
        applied_finding_ids=frozenset({"F-1"}),
        policy="not-a-real-policy",
        quiet=False,
    )
    assert rc == POST_FIX_WORKING_TREE
    err = capsys.readouterr().err
    assert "unknown --post-fix value" in err
