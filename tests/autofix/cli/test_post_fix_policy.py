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
    new_commits = [line for line in branch_log.stdout.splitlines() if line.strip()]
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

    # ``git push -u origin <branch>`` is fired before the gh call —
    # without an origin remote the test repo can't satisfy that, so
    # intercept the push at the _run_git layer.
    real_run_git = policy_mod._run_git
    git_calls: list[tuple] = []

    def _git_intercept(root, args):
        git_calls.append(tuple(args))
        if args[:1] == ["push"]:
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=0, stdout="", stderr=""
            )
        return real_run_git(root, args)

    monkeypatch.setattr(policy_mod, "_run_git", _git_intercept)

    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-pr",
        applied_finding_ids=frozenset({"F-1"}),
        policy=POST_FIX_BRANCH_PR,
        quiet=True,
    )
    assert rc == POST_FIX_BRANCH_PR
    assert captured["args"][:3] == ("gh", "pr", "create")
    assert "--head" in captured["args"]
    assert "autofix/fixes-r-pr" in captured["args"]
    # Title + body are now passed explicitly (replaces the old
    # ``--fill``-based path that left PRs as a bare commit-message
    # subject + finding-id list).
    assert "--title" in captured["args"]
    assert "--body" in captured["args"]


def test_branch_pr_pushes_branch_to_origin_before_gh_pr_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``gh pr create --head <branch>`` resolves the branch via the
    GitHub API. The branch MUST exist on the remote before the call.
    Without a push, the API errors with ``No commits between main and
    <branch>; Head ref must be a branch`` and the dispatcher reverts
    the working tree (losing the just-applied fixes).

    Pin the contract: ``git push -u origin <branch>`` MUST be called
    BEFORE ``gh pr create``.
    """
    _git_init(tmp_path)
    monkeypatch.setattr(policy_mod.shutil, "which", lambda _: "/usr/bin/gh")

    real_run_git = policy_mod._run_git
    call_log: list[tuple[str, tuple]] = []

    def _logged_git(root, args):
        call_log.append(("git", tuple(args)))
        if args[:1] == ["push"]:
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=0, stdout="", stderr=""
            )
        return real_run_git(root, args)

    def _logged_gh(root, args):
        call_log.append(("gh", tuple(args)))
        return subprocess.CompletedProcess(
            args=list(args), returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(policy_mod, "_run_git", _logged_git)
    monkeypatch.setattr(policy_mod, "_run_gh", _logged_gh)

    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-pushtest",
        applied_finding_ids=frozenset({"F-1"}),
        policy=POST_FIX_BRANCH_PR,
        quiet=True,
    )

    assert rc == POST_FIX_BRANCH_PR

    push_calls = [
        i for i, (kind, args) in enumerate(call_log)
        if kind == "git" and args[:1] == ("push",)
    ]
    gh_pr_calls = [
        i for i, (kind, args) in enumerate(call_log)
        if kind == "gh" and args[:3] == ("gh", "pr", "create")
    ]

    assert push_calls, (
        "expected at least one `git push` before `gh pr create`; "
        f"got call log: {call_log!r}"
    )
    assert gh_pr_calls, "expected a `gh pr create` call"
    assert push_calls[0] < gh_pr_calls[0], (
        "`git push` must run BEFORE `gh pr create`; "
        f"got push at index {push_calls[0]}, gh pr at {gh_pr_calls[0]}"
    )

    # Verify the push targets origin and -u sets upstream.
    push_args = call_log[push_calls[0]][1]
    assert push_args[0] == "push"
    assert "-u" in push_args
    assert "origin" in push_args
    assert "autofix/fixes-r-pushtest" in push_args


def test_branch_only_does_not_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For ``branch`` policy (no PR), do NOT push to origin —
    branch-only mode is intentionally a local-only artifact.
    """
    _git_init(tmp_path)

    real_run_git = policy_mod._run_git
    call_log: list[tuple] = []

    def _logged_git(root, args):
        call_log.append(tuple(args))
        return real_run_git(root, args)

    monkeypatch.setattr(policy_mod, "_run_git", _logged_git)

    rc = policy_mod.apply_post_fix_policy(
        root=tmp_path,
        run_id="r-branchonly",
        applied_finding_ids=frozenset({"F-1"}),
        policy=POST_FIX_BRANCH,
        quiet=True,
    )

    assert rc == POST_FIX_BRANCH
    push_calls = [c for c in call_log if c[:1] == ("push",)]
    assert not push_calls, (
        f"branch-only policy must not push; got pushes: {push_calls!r}"
    )


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


# ---------------------------------------------------------------------------
# PR body shape (replaces the old --fill-based bare-commit-message body)
# ---------------------------------------------------------------------------


def test_parse_finding_id_recovers_rule_path_lines() -> None:
    """Synthesized LLM-judgment finding-ids decompose into the
    three columns the PR body table renders.
    """
    rule, path, lines = policy_mod._parse_finding_id(
        "llm:security:command-injection@autofix/agent_loop.py#L137-154"
    )
    assert rule == "llm:security:command-injection"
    assert path == "autofix/agent_loop.py"
    assert lines == "137-154"


def test_parse_finding_id_falls_back_for_opaque_ids() -> None:
    """SHA-style finding-ids (cheap analyzers' fingerprints) have no
    embedded location, so the parser places them in the rule_id slot
    and stubs path / lines with ``-`` rather than crashing.
    """
    opaque = "deadbeef" * 8
    rule, path, lines = policy_mod._parse_finding_id(opaque)
    assert rule == opaque
    assert path == "-"
    assert lines == "-"


def test_build_pr_body_renders_all_four_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end shape pin: the body produced for a real branch
    contains Summary / Findings fixed / Diff / Verify and
    interpolates the finding count, file count, finding-id table,
    and branch name.
    """
    _git_init(tmp_path)
    # Make a fake "autofix" branch that holds a commit so ``git
    # show`` returns a real diff.
    subprocess.run(
        ["git", "checkout", "-q", "-b", "autofix/fixes-r-body"],
        cwd=tmp_path, check=True,
    )
    (tmp_path / "fixed.py").write_text("def foo(): return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "autofix: applied"],
        cwd=tmp_path, check=True,
    )

    body = policy_mod._build_pr_body(
        root=tmp_path,
        branch_name="autofix/fixes-r-body",
        applied_finding_ids=frozenset({
            "llm:security:command-injection@autofix/agent_loop.py#L137-154",
            "llm:security:path-traversal@autofix/agent_loop.py#L105-115",
        }),
    )

    # All four canonical sections.
    for heading in ("## Summary", "## Findings fixed", "## Diff", "## Verify"):
        assert heading in body, f"missing section: {heading!r}"

    # Counts interpolated correctly.
    assert "applied 2 LLM-generated fix(es)" in body
    assert "1 file(s)" in body  # both findings live in agent_loop.py

    # Findings-table rows present, table-formatted.
    assert "| `llm:security:command-injection` |" in body
    assert "`autofix/agent_loop.py:137-154`" in body
    assert "| `llm:security:path-traversal` |" in body

    # Diff fence present and contains the actual git-show output.
    assert "```diff" in body
    assert "diff --git" in body  # `git show --format=` always emits this
    assert "fixed.py" in body

    # Verify section names the branch.
    assert "autofix/fixes-r-body" in body


def test_build_pr_body_truncates_oversized_diffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diff > PR_DIFF_MAX_BYTES is clipped with a clear marker so
    the GitHub API never rejects the body for size.
    """
    from autofix.cli import post_fix_constants as c

    _git_init(tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "autofix/fixes-r-big"],
        cwd=tmp_path, check=True,
    )
    # Force a diff much larger than the cap. Keep the file under
    # 1MB so the test is fast.
    huge = "x = 1\n" * (c.PR_DIFF_MAX_BYTES // 4)
    (tmp_path / "huge.py").write_text(huge)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "huge"], cwd=tmp_path, check=True
    )

    body = policy_mod._build_pr_body(
        root=tmp_path,
        branch_name="autofix/fixes-r-big",
        applied_finding_ids=frozenset({"F-1"}),
    )

    assert "[diff truncated for GitHub body size cap" in body
    # Still under a generous safety bound: prose + table + cap + marker.
    assert len(body.encode("utf-8")) < 65_000


def test_build_pr_body_degrades_when_git_show_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``git show`` (e.g. branch missing on disk) emits a
    body with an empty diff block instead of raising — the policy
    layer's invariant is "PR opens or we revert", so the body
    builder must never be the thing that crashes the chain.
    """
    _git_init(tmp_path)
    # Reference a branch that does not exist; ``git show`` exits non-zero.
    body = policy_mod._build_pr_body(
        root=tmp_path,
        branch_name="autofix/fixes-does-not-exist",
        applied_finding_ids=frozenset({"F-1"}),
    )
    # Body still has all four headings; diff fence is just empty.
    assert "## Summary" in body
    assert "```diff\n\n```" in body or "```diff\n```" in body
