"""Markdown-fence stripping for LLM responses.

Surfaced during a real autofix dogfood run: the LLM-judgment
analyzers' prompts say "Output only the JSON list, no preamble
or explanation", but Claude (and most LLMs) frequently wrap
their output in a ``json ... `` markdown fence anyway. The base
class's ``json.loads`` choked on the leading fence, the
finding was logged as ``AnalyzerError``, and the parsed
result was silently dropped on the floor.

Real findings dropped during the dogfood (visible in
``.autofix/events.jsonl`` ``AnalyzerError`` entries):

* ``llm:code-quality`` flagged duplicated-logic + overly-broad-except
  in ``autofix/agent_loop.py``.
* ``llm:performance`` flagged O(n^2) prompt rebuilding +
  unbounded message accumulation in the same file.

Both are real bugs the LLM correctly identified — the parser
was the failure mode. This test pins the strip behavior so
those findings now flow through to the workflow.
"""
from __future__ import annotations

import json


from autofix.analyzers.llm_judgment._base import _strip_markdown_json_fence


def test_strips_explicit_json_fence() -> None:
    raw = '```json\n[{"a": 1}]\n```'
    assert _strip_markdown_json_fence(raw) == '[{"a": 1}]'


def test_strips_unlabelled_fence() -> None:
    raw = '```\n[{"a": 1}]\n```'
    assert _strip_markdown_json_fence(raw) == '[{"a": 1}]'


def test_strips_with_surrounding_whitespace() -> None:
    raw = '   ```json\n[{"a": 1}]\n```\n   '
    assert _strip_markdown_json_fence(raw) == '[{"a": 1}]'


def test_strips_when_closing_fence_missing() -> None:
    """A truncated response (no closing fence) still has its
    opening fence stripped — we recover what we can."""
    raw = '```json\n[{"a": 1}]'
    assert _strip_markdown_json_fence(raw) == '[{"a": 1}]'


def test_unfenced_response_returned_unchanged() -> None:
    """A bare JSON response (no fences) passes through verbatim."""
    raw = '[{"a": 1}]'
    assert _strip_markdown_json_fence(raw) == '[{"a": 1}]'


def test_empty_string_returned_unchanged() -> None:
    assert _strip_markdown_json_fence("") == ""


def test_strip_then_parse_real_dogfood_response() -> None:
    """Verbatim response captured from the dogfood run's events.jsonl
    must parse to a 2-finding list after stripping."""
    raw = (
        "```json\n"
        "[\n"
        "  {\n"
        '    "category": "algorithmic-complexity",\n'
        '    "severity": "major",\n'
        '    "description": "O(n^2) prompt rebuild",\n'
        '    "start_line": 199,\n'
        '    "end_line": 204,\n'
        '    "evidence": "string concat in loop"\n'
        "  },\n"
        "  {\n"
        '    "category": "memory-accumulation",\n'
        '    "severity": "major",\n'
        '    "description": "unbounded messages list",\n'
        '    "start_line": 196,\n'
        '    "end_line": 233,\n'
        '    "evidence": "messages.append in loop"\n'
        "  }\n"
        "]\n"
        "```\n"
    )
    cleaned = _strip_markdown_json_fence(raw)
    parsed = json.loads(cleaned)
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert parsed[0]["category"] == "algorithmic-complexity"
    assert parsed[1]["category"] == "memory-accumulation"


def test_strip_then_parse_empty_findings_response() -> None:
    """An empty-list response wrapped in a fence (very common —
    LLM saying 'I found nothing') must parse to ``[]``."""
    raw = "```json\n[]\n```\n"
    cleaned = _strip_markdown_json_fence(raw)
    assert json.loads(cleaned) == []


def test_partial_fence_body_only() -> None:
    """A response that LOOKS unfenced but contains a stray ``` mid-text
    is left alone."""
    raw = '[{"a": "see ```code``` here"}]'
    assert _strip_markdown_json_fence(raw) == raw
