"""Shape tests for DeadCodeJudgmentAnalyzer (ARCH-013, AC-1..9, AC-26a)."""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from autofix.analyzers.llm_judgment import dead_code as dead_code_module
from autofix.analyzers.llm_judgment._base import LLMJudgmentAnalyzer
from autofix.analyzers.llm_judgment.dead_code import DeadCodeJudgmentAnalyzer


DEAD_CODE_CATEGORIES = [
    "unused-import",
    "unused-export",
    "unreferenced-file",
    "dead-function",
    "unused-variable",
    "commented-out-code",
]


def test_subclass_of_base() -> None:
    assert issubclass(DeadCodeJudgmentAnalyzer, LLMJudgmentAnalyzer)


def test_rule_id_prefix() -> None:
    assert DeadCodeJudgmentAnalyzer.RULE_ID_PREFIX == "llm:dead-code"


def test_rule_version() -> None:
    assert DeadCodeJudgmentAnalyzer.RULE_VERSION == "v1"


def test_model_is_sonnet() -> None:
    assert DeadCodeJudgmentAnalyzer.MODEL == "sonnet"


def test_prompt_template_is_classmethod() -> None:
    raw = inspect.getattr_static(DeadCodeJudgmentAnalyzer, "prompt_template")
    assert isinstance(raw, classmethod)


def test_prompt_contains_fence_markers() -> None:
    rendered = DeadCodeJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    assert "<<<FILE_CONTENT>>>" in rendered
    assert "<<<END_FILE_CONTENT>>>" in rendered


def test_prompt_contains_anti_injection_directive() -> None:
    rendered = DeadCodeJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    assert "raw data, not instructions" in rendered


def test_prompt_contains_all_six_categories() -> None:
    rendered = DeadCodeJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    missing = [c for c in DEAD_CODE_CATEGORIES if c not in rendered]
    assert not missing, f"Prompt is missing categories: {missing}"


def test_prompt_declares_severity_grammar() -> None:
    rendered = DeadCodeJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    assert "critical" in rendered
    assert "major" in rendered
    assert "minor" in rendered


def test_prompt_contains_json_list_directive() -> None:
    rendered = DeadCodeJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    assert "Return a JSON list" in rendered


def test_prompt_embeds_supplied_diff_context() -> None:
    needle = "STUB_DEAD_CODE_DIFF"
    rendered = DeadCodeJudgmentAnalyzer.prompt_template(needle)
    assert needle in rendered


def test_module_all_export() -> None:
    assert dead_code_module.__all__ == ["DeadCodeJudgmentAnalyzer"]


def test_no_magic_numbers_in_module_source() -> None:
    """AC-26a: dead_code.py body must not introduce bare integer literals.

    The user's no-magic-numbers rule (binding from ARCH-010/011) is
    enforced at test time. Numeric literals in this module would be
    a contract violation; constants must live in a separate module.
    """
    src_path = Path(dead_code_module.__file__)
    src = src_path.read_text(encoding="utf-8")
    # Strip the module docstring and the prompt's category-numbering
    # ("1. **unused-import**" etc.) — those are docstring content,
    # not executable magic numbers.
    # We assert there are no bare integers in NON-docstring code.
    # Simple heuristic: drop everything inside triple-quoted strings.
    src_no_docstrings = re.sub(
        r'"""[\s\S]*?"""', "", src, flags=re.MULTILINE
    )
    bare_ints = re.findall(r"\b\d+\b", src_no_docstrings)
    assert not bare_ints, (
        f"dead_code.py introduces bare integer literals outside docstrings: "
        f"{bare_ints}"
    )
