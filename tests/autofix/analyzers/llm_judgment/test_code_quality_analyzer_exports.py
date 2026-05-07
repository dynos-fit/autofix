"""Pure exports check for CodeQualityJudgmentAnalyzer (AC-12 of task-20260507-004).

Verifies:
- Class-level constants are pinned to expected values.
- prompt_template is a classmethod (per the base abstract contract).
- The rendered prompt enumerates all 9 code-quality categories.
- The rendered prompt wraps the file content with the documented fence markers.
- The rendered prompt contains the exact JSON-shape instruction string.
"""

from __future__ import annotations

import inspect

from autofix.analyzers.llm_judgment.code_quality import CodeQualityJudgmentAnalyzer


def test_rule_id_prefix_is_pinned() -> None:
    """RULE_ID_PREFIX must be the documented 'llm:code-quality' identifier."""
    assert CodeQualityJudgmentAnalyzer.RULE_ID_PREFIX == "llm:code-quality"


def test_rule_version_is_v1() -> None:
    """RULE_VERSION must be 'v1' for the initial release."""
    assert CodeQualityJudgmentAnalyzer.RULE_VERSION == "v1"


def test_model_is_sonnet() -> None:
    """MODEL must be 'sonnet' for code-quality judgment."""
    assert CodeQualityJudgmentAnalyzer.MODEL == "sonnet"


def test_prompt_template_is_classmethod() -> None:
    """prompt_template must be defined as a classmethod (not a regular function or staticmethod)."""
    raw = inspect.getattr_static(CodeQualityJudgmentAnalyzer, "prompt_template")
    assert isinstance(raw, classmethod), (
        f"prompt_template must be a classmethod, got {type(raw).__name__}"
    )


def test_prompt_contains_all_nine_categories() -> None:
    """The rendered prompt must enumerate every one of the 9 supported categories."""
    rendered = CodeQualityJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    assert isinstance(rendered, str)

    expected_categories = [
        "error-handling-gap",
        "dead-branch",
        "magic-number",
        "unclear-name",
        "overly-broad-except",
        "missing-docstring",
        "complexity-creep",
        "duplicated-logic",
        "boundary-validation-missing",
    ]
    missing = [cat for cat in expected_categories if cat not in rendered]
    assert not missing, f"Prompt is missing categories: {missing}"


def test_prompt_contains_fence_markers() -> None:
    """The rendered prompt must wrap the file content in the documented fence markers."""
    rendered = CodeQualityJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    assert "<<<FILE_CONTENT>>>" in rendered
    assert "<<<END_FILE_CONTENT>>>" in rendered


def test_prompt_contains_json_shape_instruction() -> None:
    """The rendered prompt must contain the literal JSON-shape instruction string."""
    rendered = CodeQualityJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    expected = (
        "Return a JSON list. Each item must have keys: "
        "category, severity, description, start_line, end_line, evidence."
    )
    assert expected in rendered


def test_prompt_embeds_supplied_diff_context() -> None:
    """The supplied diff_context must appear within the rendered prompt body.

    Guards against a regression where the template stops interpolating its argument
    (e.g. accidentally returning a constant string).
    """
    needle = "def foo(): pass\n"
    rendered = CodeQualityJudgmentAnalyzer.prompt_template(needle)
    assert needle in rendered
