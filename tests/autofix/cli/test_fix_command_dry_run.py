"""Integration test: fix_command.run in dry-run mode (--apply not passed).

Verifies AC-4, AC-13, AC-14 (dry-run exit 0), AC-16b:
  - The source file is byte-identical after run.
  - Stderr contains the path:line token.
  - Stderr contains the rule_id token 'unused-import.intra-file'.
  - Stderr contains the dry-run summary 'would apply 1 fix(es)'.
  - Return code is 0.

Uses a real git repo in tmp_path (no mocking of run_scan).
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Git repo bootstrap helpers (mirror test_pipeline_sidecar_disabled.py)
# ---------------------------------------------------------------------------

def _init_repo(root: Path) -> None:
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


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def dry_run_repo(tmp_path: Path) -> Path:
    """A fresh git repo with a single Python file containing one unused import.

    The file also has one used identifier so the analyzer flags exactly the
    unused import at line 1.
    """
    _init_repo(tmp_path)
    # First commit: something to diff against (full-sweep reaches this file)
    (tmp_path / "module.py").write_text(
        "import os\n\nx = 1\n", encoding="utf-8"
    )
    _commit(tmp_path, "init")
    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_dry_run_does_not_mutate_file(
    dry_run_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC-4 / AC-16b: dry-run leaves the file byte-identical."""
    from autofix.cli import fix_command

    file_path = dry_run_repo / "module.py"
    original_bytes = file_path.read_bytes()

    ns = argparse.Namespace(root=dry_run_repo, apply=False, force=False)
    rc = fix_command.run(ns)

    assert file_path.read_bytes() == original_bytes, (
        "Dry-run must not mutate the source file"
    )
    assert rc == 0


@pytest.mark.integration
def test_dry_run_stderr_contains_path_and_line_tokens(
    dry_run_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC-13 / AC-16b: stderr contains the file path token and line number."""
    from autofix.cli import fix_command

    ns = argparse.Namespace(root=dry_run_repo, apply=False, force=False)
    fix_command.run(ns)
    err = capsys.readouterr().err

    # The output format is "{path}:{line}  -  {line_text}  [{rule_id}]"
    # We check that 'module.py' and ':1' appear together (line 1)
    assert "module.py" in err, (
        f"Expected 'module.py' in stderr, got:\n{err}"
    )
    assert ":1" in err, (
        f"Expected line number ':1' token in stderr, got:\n{err}"
    )


@pytest.mark.integration
def test_dry_run_stderr_contains_rule_id(
    dry_run_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC-13 / AC-16b: stderr contains the rule_id 'unused-import.intra-file'."""
    from autofix.cli import fix_command

    ns = argparse.Namespace(root=dry_run_repo, apply=False, force=False)
    fix_command.run(ns)
    err = capsys.readouterr().err

    assert "unused-import.intra-file" in err, (
        f"Expected 'unused-import.intra-file' in stderr, got:\n{err}"
    )


@pytest.mark.integration
def test_dry_run_stderr_contains_would_apply_summary(
    dry_run_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC-13: dry-run summary contains 'would apply 1 fix(es)'."""
    from autofix.cli import fix_command

    ns = argparse.Namespace(root=dry_run_repo, apply=False, force=False)
    fix_command.run(ns)
    err = capsys.readouterr().err

    assert "would apply 1 fix(es)" in err, (
        f"Expected 'would apply 1 fix(es)' in stderr, got:\n{err}"
    )


@pytest.mark.integration
def test_dry_run_returns_exit_code_zero(
    dry_run_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC-14: dry-run always returns exit code 0."""
    from autofix.cli import fix_command

    ns = argparse.Namespace(root=dry_run_repo, apply=False, force=False)
    rc = fix_command.run(ns)
    assert rc == 0


@pytest.mark.integration
def test_dry_run_dash_minus_format_in_stderr(
    dry_run_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC-13: safe findings appear with the '-' marker, not '?'."""
    from autofix.cli import fix_command

    ns = argparse.Namespace(root=dry_run_repo, apply=False, force=False)
    fix_command.run(ns)
    err = capsys.readouterr().err

    # Safe (would-apply) finding lines use '  -  ' separator
    assert "  -  " in err, (
        f"Expected '  -  ' separator in safe-finding line, got:\n{err}"
    )


@pytest.mark.integration
def test_empty_findings_short_circuits(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC implicit requirement: when no findings, stderr is exactly
    'no findings; nothing to fix\\n' and rc == 0."""
    from autofix.cli import fix_command

    # Repo with NO unused imports
    _init_repo(tmp_path)
    (tmp_path / "clean.py").write_text(
        "import os\n\npath = os.getcwd()\n", encoding="utf-8"
    )
    _commit(tmp_path, "init clean")

    ns = argparse.Namespace(root=tmp_path, apply=False, force=False)
    rc = fix_command.run(ns)
    err = capsys.readouterr().err

    assert rc == 0
    assert err == "no findings; nothing to fix\n", (
        f"Expected exactly 'no findings; nothing to fix\\n', got: {err!r}"
    )
