"""TDD-first: assert no autofix/legacy-rewrite namespace remnants.

Covers:
- AC 1: directory `autofix/` does not exist after the cutover.
- AC 8: directory `docs/rewrite/` does not exist after the cutover.
- AC 13: literal string for the old namespace does not appear in any *.py,
  *.md, *.sh, or pyproject.toml (excluding .dynos/, .git/, .venv/,
  *.egg-info/).

These tests MUST FAIL until task-20260506-003 lands.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


_LEGACY_NEEDLE = "autofix" + "_next"  # split to avoid self-match


def test_legacy_namespace_directory_does_not_exist() -> None:
    """AC 1: the autofix/ source directory is gone."""
    assert not (REPO_ROOT / ("autofix" + "_next")).exists(), (
        "autofix/ still exists; the rename has not happened."
    )


def test_docs_rewrite_directory_does_not_exist() -> None:
    """AC 8: docs/rewrite/ is deleted."""
    assert not (REPO_ROOT / "docs" / "rewrite").exists(), (
        "docs/rewrite/ still exists."
    )


def test_demo_llm_prompt_does_not_exist() -> None:
    """AC 7: demo_llm_prompt.py is deleted."""
    assert not (REPO_ROOT / "demo_llm_prompt.py").exists()


def test_no_legacy_namespace_in_source_or_docs() -> None:
    """AC 13: no source/doc file contains the literal old namespace."""
    proc = subprocess.run(
        [
            "grep",
            "-r",
            "-l",
            "--include=*.py",
            "--include=*.md",
            "--include=*.sh",
            "--include=*.toml",
            "--exclude-dir=.dynos",
            "--exclude-dir=.git",
            "--exclude-dir=.venv",
            "--exclude-dir=__pycache__",
            "--exclude=*.egg-info*",
            "--exclude=CHANGELOG.md",
            "--exclude=test_output_paths.py",
            "--exclude=test_scip_index_real_repo.py",
            _LEGACY_NEEDLE,
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
    )
    # grep returns 1 when no matches (success for us). Returns 0 when matches found.
    if proc.returncode == 1:
        return
    if proc.returncode != 0:
        pytest.fail(f"grep failed: rc={proc.returncode} stderr={proc.stderr!r}")
    # rc == 0: there are matches. Filter out this very test file (which contains
    # _LEGACY_NEEDLE construction comments).
    lines = [
        ln for ln in proc.stdout.splitlines()
        if ln and Path(ln).resolve() != Path(__file__).resolve()
    ]
    assert not lines, (
        "files still contain the old namespace:\n  " + "\n  ".join(lines)
    )
