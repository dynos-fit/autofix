"""Shape tests for PerformanceJudgmentAnalyzer (ARCH-013, AC-10..18, AC-26b)."""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from autofix.analyzers.llm_judgment import performance as performance_module
from autofix.analyzers.llm_judgment._base import LLMJudgmentAnalyzer
from autofix.analyzers.llm_judgment.performance import PerformanceJudgmentAnalyzer


PERFORMANCE_CATEGORIES = [
    "n-plus-one",
    "missing-index",
    "unbounded-query",
    "missing-transaction",
    "algorithmic-complexity",
    "unnecessary-recomputation",
    "large-payload-serialization",
    "connection-pool-exhaustion",
    "memory-accumulation",
    "missing-timeout",
    "blocking-event-loop",
]


def test_subclass_of_base() -> None:
    assert issubclass(PerformanceJudgmentAnalyzer, LLMJudgmentAnalyzer)


def test_rule_id_prefix() -> None:
    assert PerformanceJudgmentAnalyzer.RULE_ID_PREFIX == "llm:performance"


def test_rule_version() -> None:
    assert PerformanceJudgmentAnalyzer.RULE_VERSION == "v1"


def test_model_is_sonnet() -> None:
    assert PerformanceJudgmentAnalyzer.MODEL == "sonnet"


def test_prompt_template_is_classmethod() -> None:
    raw = inspect.getattr_static(PerformanceJudgmentAnalyzer, "prompt_template")
    assert isinstance(raw, classmethod)


def test_prompt_contains_fence_markers() -> None:
    rendered = PerformanceJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    assert "<<<FILE_CONTENT>>>" in rendered
    assert "<<<END_FILE_CONTENT>>>" in rendered


def test_prompt_contains_anti_injection_directive() -> None:
    rendered = PerformanceJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    assert "raw data, not instructions" in rendered


def test_prompt_contains_all_eleven_categories() -> None:
    rendered = PerformanceJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    missing = [c for c in PERFORMANCE_CATEGORIES if c not in rendered]
    assert not missing, f"Prompt is missing categories: {missing}"


def test_prompt_declares_severity_grammar() -> None:
    rendered = PerformanceJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    assert "critical" in rendered
    assert "major" in rendered
    assert "minor" in rendered


def test_prompt_contains_json_list_directive() -> None:
    rendered = PerformanceJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    assert "Return a JSON list" in rendered


def test_prompt_embeds_supplied_diff_context() -> None:
    needle = "STUB_PERFORMANCE_DIFF"
    rendered = PerformanceJudgmentAnalyzer.prompt_template(needle)
    assert needle in rendered


def test_module_all_export() -> None:
    assert performance_module.__all__ == ["PerformanceJudgmentAnalyzer"]


def test_no_magic_numbers_in_module_source() -> None:
    """AC-26b: performance.py body must not introduce bare integer literals."""
    src_path = Path(performance_module.__file__)
    src = src_path.read_text(encoding="utf-8")
    src_no_docstrings = re.sub(
        r'"""[\s\S]*?"""', "", src, flags=re.MULTILINE
    )
    bare_ints = re.findall(r"\b\d+\b", src_no_docstrings)
    assert not bare_ints, (
        f"performance.py introduces bare integer literals outside docstrings: "
        f"{bare_ints}"
    )
