"""Pure exports check for SecurityJudgmentAnalyzer (AC-12 of task-20260507-008).

Verifies:
- Class-level constants are pinned to expected values (RULE_ID_PREFIX,
  RULE_VERSION, MODEL).
- prompt_template is a classmethod (per the base abstract contract).
- The rendered prompt enumerates all 9 OWASP-style security categories.
- The rendered prompt wraps the file content with the documented fence markers.
- The rendered prompt contains the exact JSON-shape instruction string.
- The rendered prompt contains the severity-convention instruction.
- The rendered prompt interpolates the supplied diff_context.
- The module docstring documents all 9 categories (AC-13).
"""

from __future__ import annotations

import inspect

from autofix.analyzers.llm_judgment import security as security_module
from autofix.analyzers.llm_judgment.security import SecurityJudgmentAnalyzer


def test_rule_id_prefix_is_pinned() -> None:
    """RULE_ID_PREFIX must be the documented 'llm:security' identifier."""
    assert SecurityJudgmentAnalyzer.RULE_ID_PREFIX == "llm:security"


def test_rule_version_is_v1() -> None:
    """RULE_VERSION must be 'v1' for the initial release."""
    assert SecurityJudgmentAnalyzer.RULE_VERSION == "v1"


def test_model_is_opus() -> None:
    """MODEL must be 'opus' for security judgment (high-stakes / low-volume)."""
    assert SecurityJudgmentAnalyzer.MODEL == "opus"


def test_prompt_template_is_classmethod() -> None:
    """prompt_template must be defined as a classmethod (not a regular function or staticmethod)."""
    raw = inspect.getattr_static(SecurityJudgmentAnalyzer, "prompt_template")
    assert isinstance(raw, classmethod), (
        f"prompt_template must be a classmethod, got {type(raw).__name__}"
    )


def test_prompt_contains_all_nine_categories() -> None:
    """The rendered prompt must enumerate every one of the 9 supported security categories."""
    rendered = SecurityJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    assert isinstance(rendered, str)

    expected_categories = [
        "path-traversal",
        "sql-injection",
        "command-injection",
        "secret-leak",
        "auth-bypass",
        "unsafe-deserialization",
        "crypto-misuse",
        "prompt-injection",
        "data-exposure",
    ]
    missing = [cat for cat in expected_categories if cat not in rendered]
    assert not missing, f"Prompt is missing categories: {missing}"


def test_prompt_contains_fence_markers() -> None:
    """The rendered prompt must wrap the file content in the documented fence markers."""
    rendered = SecurityJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    assert "<<<FILE_CONTENT>>>" in rendered
    assert "<<<END_FILE_CONTENT>>>" in rendered


def test_prompt_contains_json_shape_instruction() -> None:
    """The rendered prompt must contain the literal JSON-shape instruction string."""
    rendered = SecurityJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    expected = (
        "Return a JSON list. Each item must have keys: "
        "category, severity, description, start_line, end_line, evidence."
    )
    assert expected in rendered


def test_prompt_contains_severity_convention() -> None:
    """The rendered prompt must contain the literal severity-convention instruction."""
    rendered = SecurityJudgmentAnalyzer.prompt_template("def foo(): pass\n")
    expected = "severity is one of: critical, major, minor."
    assert expected in rendered


def test_prompt_embeds_supplied_diff_context() -> None:
    """The supplied diff_context must appear within the rendered prompt body.

    Guards against a regression where the template stops interpolating its
    argument (e.g. accidentally returning a constant string).
    """
    needle = "STUB_DIFF"
    rendered = SecurityJudgmentAnalyzer.prompt_template(needle)
    assert needle in rendered


def test_module_docstring_documents_nine_categories() -> None:
    """The module-level docstring must document each of the 9 security categories.

    AC-13: each category gets a one-line description in the module docstring.
    """
    docstring = security_module.__doc__
    assert docstring is not None, "security.py must have a module docstring"

    expected_categories = [
        "path-traversal",
        "sql-injection",
        "command-injection",
        "secret-leak",
        "auth-bypass",
        "unsafe-deserialization",
        "crypto-misuse",
        "prompt-injection",
        "data-exposure",
    ]
    missing = [cat for cat in expected_categories if cat not in docstring]
    assert not missing, f"Module docstring is missing categories: {missing}"
