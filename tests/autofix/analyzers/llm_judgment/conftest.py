"""Fixtures for LLM judgment analyzer tests."""

from __future__ import annotations

import pytest

from autofix.analyzers.llm_judgment._base import LLMJudgmentAnalyzer
from autofix.parsing.tree_sitter import ParseResult


class FakeJudgmentAnalyzer(LLMJudgmentAnalyzer):
    """Fake LLM judgment analyzer for testing."""

    RULE_ID_PREFIX = "llm:fake"
    MODEL = "fake-model"

    @classmethod
    def prompt_template(cls, diff_context: str) -> str:
        """Return a simple fake prompt."""
        return f"[fake]\n{diff_context}"


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset per-scan state before and after each test."""
    yield
    LLMJudgmentAnalyzer._reset_per_scan_state()


@pytest.fixture
def parse_result(tmp_path):
    """Create a minimal ParseResult-shaped stub for testing.

    The real ``autofix.parsing.tree_sitter.ParseResult`` exposes
    ``path`` (absolute) and ``relpath`` (repo-relative). The base
    analyzer derives ``repo_root`` via :func:`_resolve_repo_root`
    (= ``path``-minus-``relpath``-components). Tests must use the
    same surface so the resolver is exercised end-to-end.
    """
    src = tmp_path / "p.py"
    src.write_text("x = 1\n", encoding="utf-8")

    class FakeParseResult:
        # ``path`` and ``relpath`` are the production-shaped fields the
        # base analyzer actually reads. ``repo_root`` is a convenience
        # alias many tests still use for cache-path arithmetic — it
        # mirrors what :func:`_resolve_repo_root` would derive from
        # ``path`` + ``relpath``, so test computation matches what the
        # production code computes.
        path = src
        relpath = "p.py"
        repo_root = tmp_path

    return FakeParseResult()
