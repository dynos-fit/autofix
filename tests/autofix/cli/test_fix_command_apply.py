"""Integration tests: fix_command.run with --apply.

Covers AC-12, AC-13, AC-14, AC-16c:

  test_apply_removes_only_safe_import
      One safe import, one multi-name import, one noqa import, one used line.
      After apply: safe import line absent, multi-name and noqa lines intact,
      non-import lines intact. Summary: 'applied 1 fix(es) across 1 file(s);
      2 skipped (1 unsafe-multiname, 1 noqa)'.

  test_apply_dirty_tree_refuses
      Tracked file modified (not staged). With apply=True, force=False.
      Expects rc == 2 and stderr names dirty path count.

  test_apply_dirty_tree_with_force_proceeds
      Same dirty setup but force=True. Expects rc == 0 and safe import removed.

  test_empty_findings_short_circuits (apply mode)
      Repo with no unused imports, apply=True. Expects rc == 0 and exactly
      'no findings; nothing to fix\\n' on stderr.

Uses a real git repo in tmp_path (no mocking of run_scan).
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Git repo bootstrap helpers
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
# The canonical mixed-import content used across several tests.
# Line structure (1-indexed):
#   1: import json          <- safe (single-name, no noqa, unused)
#   2: from pathlib import Path, PurePath   <- unsafe-multiname (neither used)
#   3: import sys  # noqa: F401            <- noqa (single-name but suppressed)
#   4: (blank)
#   5: x = 1               <- used, must survive
# ---------------------------------------------------------------------------

_MIXED_CONTENT = (
    "import json\n"
    "from pathlib import Path, PurePath\n"
    "import sys  # noqa: F401\n"
    "\n"
    "x = 1\n"
)


@pytest.fixture
def mixed_repo(tmp_path: Path) -> Path:
    """A fresh git repo with the canonical mixed-import file, committed."""
    _init_repo(tmp_path)
    (tmp_path / "target.py").write_bytes(_MIXED_CONTENT.encode())
    _commit(tmp_path, "init")
    return tmp_path


# ---------------------------------------------------------------------------
# AC-16c primary test
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_apply_removes_only_safe_import(
    mixed_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC-12 / AC-16c: only the safe single-name import is removed.

    The multi-name and noqa imports must survive byte-identical.
    The summary must read:
      'applied 1 fix(es) across 1 file(s); 2 skipped (1 unsafe-multiname, 1 noqa)'
    """
    from autofix.cli import fix_command

    file_path = mixed_repo / "target.py"

    ns = argparse.Namespace(root=mixed_repo, apply=True, force=False)
    rc = fix_command.run(ns)
    err = capsys.readouterr().err

    assert rc == 0, f"Expected rc 0, got {rc}. Stderr:\n{err}"

    # File must no longer contain 'import json\n'
    content_after = file_path.read_bytes().decode("utf-8")
    assert "import json\n" not in content_after, (
        f"Safe import line should have been deleted, but file now contains:\n{content_after}"
    )

    # Multi-name line must be byte-present
    assert "from pathlib import Path, PurePath\n" in content_after, (
        f"Multi-name import line should be preserved, file now:\n{content_after}"
    )

    # noqa line must be byte-present
    assert "import sys  # noqa: F401\n" in content_after, (
        f"noqa import line should be preserved, file now:\n{content_after}"
    )

    # Non-import line must be byte-present
    assert "x = 1\n" in content_after, (
        f"Non-import used line should be preserved, file now:\n{content_after}"
    )

    # Summary line
    assert "applied 1 fix(es) across 1 file(s); 2 skipped (1 unsafe-multiname, 1 noqa)" in err, (
        f"Expected exact summary in stderr, got:\n{err}"
    )


@pytest.mark.integration
def test_apply_safe_import_line_absent_from_bytes(
    mixed_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC-12: after apply, the removed line is not present even at byte level."""
    from autofix.cli import fix_command

    file_path = mixed_repo / "target.py"
    ns = argparse.Namespace(root=mixed_repo, apply=True, force=False)
    fix_command.run(ns)
    capsys.readouterr()

    raw = file_path.read_bytes()
    assert b"import json\n" not in raw, (
        "import json line must be removed at byte level"
    )


@pytest.mark.integration
def test_apply_preserved_lines_are_byte_identical(
    mixed_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC-12: the surviving lines (multi-name, noqa, x=1) are byte-identical
    to the originals (line endings not mangled)."""
    from autofix.cli import fix_command

    file_path = mixed_repo / "target.py"
    ns = argparse.Namespace(root=mixed_repo, apply=True, force=False)
    fix_command.run(ns)
    capsys.readouterr()

    raw = file_path.read_bytes()
    assert b"from pathlib import Path, PurePath\n" in raw
    assert b"import sys  # noqa: F401\n" in raw
    assert b"x = 1\n" in raw


@pytest.mark.integration
def test_apply_stderr_marks_unsafe_with_question_mark(
    mixed_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC-13: skipped findings appear with '  ?  ' and [skipped: unsafe-multiname]
    or [skipped: noqa]."""
    from autofix.cli import fix_command

    ns = argparse.Namespace(root=mixed_repo, apply=True, force=False)
    fix_command.run(ns)
    err = capsys.readouterr().err

    assert "  ?  " in err, (
        f"Expected '  ?  ' for skipped findings in stderr:\n{err}"
    )
    assert "skipped: unsafe-multiname" in err, (
        f"Expected '[skipped: unsafe-multiname]' in stderr:\n{err}"
    )
    assert "skipped: noqa" in err, (
        f"Expected '[skipped: noqa]' in stderr:\n{err}"
    )


# ---------------------------------------------------------------------------
# Dirty-tree refusal tests (AC-11, AC-14)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_apply_dirty_tree_refuses(
    mixed_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC-11 / AC-14: apply on a dirty tree without --force returns rc=2
    and stderr names the dirty path count."""
    from autofix.cli import fix_command

    # Modify a tracked file without committing
    (mixed_repo / "target.py").write_text(
        _MIXED_CONTENT + "# dirty modification\n", encoding="utf-8"
    )
    # Verify tree is indeed dirty
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=mixed_repo, capture_output=True, text=True,
    )
    assert result.stdout.strip(), "Expected dirty tree for test setup"

    file_bytes_before = (mixed_repo / "target.py").read_bytes()

    ns = argparse.Namespace(root=mixed_repo, apply=True, force=False)
    rc = fix_command.run(ns)
    err = capsys.readouterr().err

    assert rc == 2, f"Expected rc=2 for dirty tree, got {rc}. Stderr:\n{err}"

    # Stderr must mention a dirty path count (e.g. "1 modified path(s)"
    # or "1 path(s)" or similar — any digit + path-related word)
    import re
    assert re.search(r"\d+\s+\S*path", err), (
        f"Expected dirty path count in stderr, got:\n{err}"
    )

    # File must NOT have been mutated by the refused apply
    assert (mixed_repo / "target.py").read_bytes() == file_bytes_before, (
        "Dirty-tree refusal must not mutate any file"
    )


@pytest.mark.integration
def test_apply_dirty_tree_with_force_proceeds(
    mixed_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC-5 / AC-11 / AC-14: --force overrides dirty-tree refusal.
    rc == 0 and the safe import is removed."""
    from autofix.cli import fix_command

    # Make tree dirty
    (mixed_repo / "target.py").write_bytes(_MIXED_CONTENT.encode())
    (mixed_repo / "unrelated.py").write_text("# dirty unrelated\n", encoding="utf-8")

    ns = argparse.Namespace(root=mixed_repo, apply=True, force=True)
    rc = fix_command.run(ns)
    err = capsys.readouterr().err

    assert rc == 0, f"Expected rc=0 with --force, got {rc}. Stderr:\n{err}"

    content_after = (mixed_repo / "target.py").read_bytes().decode("utf-8")
    assert "import json\n" not in content_after, (
        f"Safe import should be removed even on dirty tree with --force:\n{content_after}"
    )


# ---------------------------------------------------------------------------
# Empty findings short-circuit (apply mode) (AC implicit requirement)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_empty_findings_apply_mode_short_circuits(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """AC implicit: 'no findings; nothing to fix\\n' when repo is clean,
    even in --apply mode. rc == 0."""
    from autofix.cli import fix_command

    _init_repo(tmp_path)
    (tmp_path / "clean.py").write_text(
        "import os\n\npath = os.getcwd()\n", encoding="utf-8"
    )
    _commit(tmp_path, "init clean")

    ns = argparse.Namespace(root=tmp_path, apply=True, force=False)
    rc = fix_command.run(ns)
    err = capsys.readouterr().err

    assert rc == 0
    assert err == "no findings; nothing to fix\n", (
        f"Expected exactly 'no findings; nothing to fix\\n', got: {err!r}"
    )
