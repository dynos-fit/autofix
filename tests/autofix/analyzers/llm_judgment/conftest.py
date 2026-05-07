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
    """Create a minimal ParseResult for testing."""
    src = tmp_path / "p.py"
    src.write_text("x = 1\n", encoding="utf-8")

    class FakeParseResult:
        repo_root = tmp_path
        relpath = "p.py"

    return FakeParseResult()
