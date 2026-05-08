"""Regression test for the ParseResult.repo_root attribute crash.

Earlier the LLM-judgment base class accessed
``parse_result.repo_root`` directly, but ``ParseResult`` exposes only
``path`` and ``relpath`` — no ``repo_root`` attribute. The bug
survived test coverage because every test fixture used a fake stub
that DID expose ``repo_root``; nothing exercised the base class
against a real ``ParseResult`` from ``parse_file``.

This regression test exercises ``_resolve_repo_root`` against a
real ``parse_file`` output for files at multiple nesting depths,
asserting the invariant ``derived_repo_root / parse_result.relpath
== parse_result.path``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from autofix.analyzers.llm_judgment._base import _resolve_repo_root
from autofix.parsing.tree_sitter import parse_file


@pytest.fixture
def temp_repo(tmp_path: Path) -> Path:
    """Use ``tmp_path`` as the simulated repo root."""
    return tmp_path


def _make(repo: Path, relpath: str, content: str = "x = 1\n") -> Path:
    abs_path = repo / relpath
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content)
    return abs_path


def test_top_level_file(temp_repo: Path) -> None:
    """Single-component relpath: repo_root is the file's parent."""
    src = _make(temp_repo, "top.py")
    pr = parse_file(src, repo_root=temp_repo)
    derived = _resolve_repo_root(pr)
    assert derived / pr.relpath == pr.path


def test_one_level_nested(temp_repo: Path) -> None:
    src = _make(temp_repo, "pkg/mod.py")
    pr = parse_file(src, repo_root=temp_repo)
    derived = _resolve_repo_root(pr)
    assert derived / pr.relpath == pr.path


def test_three_levels_nested(temp_repo: Path) -> None:
    """Deep nesting: walks the parents stack the right number of steps."""
    src = _make(temp_repo, "a/b/c/file.py")
    pr = parse_file(src, repo_root=temp_repo)
    derived = _resolve_repo_root(pr)
    assert derived / pr.relpath == pr.path


def test_six_levels_nested(temp_repo: Path) -> None:
    """Pathologically deep nesting still resolves correctly."""
    src = _make(temp_repo, "a/b/c/d/e/f/file.py")
    pr = parse_file(src, repo_root=temp_repo)
    derived = _resolve_repo_root(pr)
    assert derived / pr.relpath == pr.path


def test_real_parse_result_has_no_repo_root_attr() -> None:
    """Pin the ParseResult API contract: there is NO repo_root field.

    If a future change to ``parsing/tree_sitter.py`` adds a
    ``repo_root`` attribute, that's fine — but until then, code in
    the LLM-judgment path MUST NOT assume it exists. This guard
    makes the invariant a deliberate API change instead of a silent
    drift.
    """
    from autofix.parsing.tree_sitter import ParseResult

    annotations = ParseResult.__annotations__
    assert "repo_root" not in annotations, (
        "ParseResult exposes a repo_root field — the _resolve_repo_root "
        "workaround in autofix/analyzers/llm_judgment/_base.py is no "
        "longer needed and can be removed."
    )
