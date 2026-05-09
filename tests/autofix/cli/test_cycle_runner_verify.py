"""Cheap VERIFYING in the dispatcher (post-#75 follow-up).

Catches false-positive applies — e.g., the LLM-dead-code analyzer
flagging an in-use import, whose deletion breaks the file. Without
this, autofix can commit broken code on its own demo.

The verify is cheap: byte-compile every modified Python file. If
any file doesn't compile, revert the working tree and skip the
post-fix commit.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _git_init(tmp_path: Path, contents: dict[str, str]) -> Path:
    """Init a tmp git repo with given files committed at HEAD."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.invalid"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    for relpath, body in contents.items():
        p = tmp_path / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_verify_passes_when_all_files_compile(tmp_path: Path) -> None:
    """Modified files that compile cleanly → verify returns True."""
    from autofix.cli.cycle_runner import _verify_modified_files_compile

    _git_init(tmp_path, {"a.py": "x = 1\n"})
    # Modify a.py to a still-valid form.
    (tmp_path / "a.py").write_text("x = 2\ny = 3\n")
    assert _verify_modified_files_compile(tmp_path, quiet=True) is True


def test_verify_fails_on_syntax_error(tmp_path: Path) -> None:
    """Modified file with a syntax error → verify returns False."""
    from autofix.cli.cycle_runner import _verify_modified_files_compile

    _git_init(tmp_path, {"a.py": "x = 1\n"})
    (tmp_path / "a.py").write_text("x = (\n")  # truncated, syntax error
    assert _verify_modified_files_compile(tmp_path, quiet=True) is False


def test_verify_passes_with_no_modifications(tmp_path: Path) -> None:
    """A clean working tree trivially passes verify."""
    from autofix.cli.cycle_runner import _verify_modified_files_compile

    _git_init(tmp_path, {"a.py": "x = 1\n"})
    assert _verify_modified_files_compile(tmp_path, quiet=True) is True


def test_verify_logs_failure_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from autofix.cli.cycle_runner import _verify_modified_files_compile

    _git_init(tmp_path, {"a.py": "x = 1\n"})
    (tmp_path / "a.py").write_text("x = (\n")
    _verify_modified_files_compile(tmp_path, quiet=False)
    err = capsys.readouterr().err
    assert "doesn't compile" in err
    assert "a.py" in err


def test_verify_quiet_suppresses_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from autofix.cli.cycle_runner import _verify_modified_files_compile

    _git_init(tmp_path, {"a.py": "x = 1\n"})
    (tmp_path / "a.py").write_text("x = (\n")
    _verify_modified_files_compile(tmp_path, quiet=True)
    err = capsys.readouterr().err
    assert err == ""


def test_revert_working_tree_clears_modifications(tmp_path: Path) -> None:
    from autofix.cli.cycle_runner import _revert_working_tree

    _git_init(tmp_path, {"a.py": "x = 1\n"})
    (tmp_path / "a.py").write_text("CORRUPTED\n")
    assert (tmp_path / "a.py").read_text() == "CORRUPTED\n"

    _revert_working_tree(tmp_path, quiet=True)
    assert (tmp_path / "a.py").read_text() == "x = 1\n"


# --- end-to-end dispatcher behavior with verify wired in ----------------


def _fake_finding() -> object:
    return MagicMock(
        rule_id="llm:dead-code:unused-import",
        path="a.py",
        start_line=1, end_line=1,
        finding_id="F-0",
    )


def test_dispatcher_skips_post_fix_when_verify_fails(tmp_path: Path) -> None:
    """``_run_fix_core`` applied a fix; verify catches it as broken;
    dispatcher reverts and does NOT call post-fix."""
    from autofix.cli.cycle_runner import _dispatch_repair_workflow

    _git_init(tmp_path, {"a.py": "x = 1\n"})
    # Simulate a "bad apply" — _run_fix_core returns success but
    # leaves the working tree with broken code.
    def _fake_fix_core(*, root, **_kwargs):
        # Sneakily corrupt the file as if a bad LLM patch was applied.
        (root / "a.py").write_text("x = (\n")
        result = MagicMock()
        result.exit_code = 0
        result.applied_finding_ids = ["F-0"]
        return result

    sentinel_post_fix = MagicMock()

    with patch("autofix.cli.fix_command._run_fix_core",
               side_effect=_fake_fix_core), \
         patch("autofix.cli.post_fix_policy.apply_post_fix_policy",
               side_effect=sentinel_post_fix):
        rc = _dispatch_repair_workflow(
            root=tmp_path, mode="commit",
            analyzers=["llm:dead-code"], quiet=True,
            findings=[_fake_finding()],
        )

    assert rc == 1
    sentinel_post_fix.assert_not_called()
    # Working tree was reverted.
    assert (tmp_path / "a.py").read_text() == "x = 1\n"


def test_dispatcher_proceeds_to_post_fix_when_verify_passes(tmp_path: Path) -> None:
    """Apply produces valid code → verify passes → post-fix called."""
    from autofix.cli.cycle_runner import _dispatch_repair_workflow

    _git_init(tmp_path, {"a.py": "x = 1\n"})

    def _fake_fix_core(*, root, **_kwargs):
        # Valid mutation.
        (root / "a.py").write_text("x = 2\n")
        result = MagicMock()
        result.exit_code = 0
        result.applied_finding_ids = ["F-0"]
        return result

    with patch("autofix.cli.fix_command._run_fix_core",
               side_effect=_fake_fix_core), \
         patch("autofix.cli.post_fix_policy.apply_post_fix_policy",
               return_value="branch") as mock_post:
        rc = _dispatch_repair_workflow(
            root=tmp_path, mode="commit",
            analyzers=["llm:dead-code"], quiet=True,
            findings=[_fake_finding()],
        )

    assert rc == 0
    mock_post.assert_called_once()


# --- LLM patcher timeout handling ---------------------------------------


def test_llm_patcher_swallows_subprocess_timeout(tmp_path: Path) -> None:
    """The LLM patcher's call to ``Scheduler.invoke_judgment`` no
    longer propagates ``TimeoutExpired`` — it returns ``None``
    (no patch) so the outer fix loop continues.
    """
    from autofix.repair import RepairTier
    from autofix.repair.llm_patcher import produce_patch

    _git_init(tmp_path, {"a.py": "x = 1\n"})

    finding = MagicMock(
        rule_id="llm:code-quality:duplicated-logic",
        path="a.py",
        start_line=1, end_line=1,
        finding_id="F-0",
        changed_slice="dup",
    )
    task = MagicMock(finding=finding, tier=RepairTier.LLM_PATCH)

    fake_scheduler = MagicMock()
    fake_scheduler.invoke_judgment.side_effect = subprocess.TimeoutExpired(
        cmd=["claude"], timeout=60,
    )

    with patch("autofix.repair.llm_patcher.Scheduler",
               return_value=fake_scheduler):
        result = produce_patch(task, root=tmp_path)

    assert result is None  # patcher gave up cleanly, didn't crash
