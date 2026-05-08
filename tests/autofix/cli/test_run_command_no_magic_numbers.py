"""Grep-based no-magic-numbers test for run_command.py (AC-4 / AC-17.b).

Reads the source bytes of the orchestration module and asserts that
forbidden literal forms (raw 3, 0.6, "0"*64, the timestamp format, the
recovery-branch prefix) do NOT appear in the body. Imports may
reference them by name, but inline literals defeat the centralized
constants discipline.
"""
from __future__ import annotations

import re
from pathlib import Path


_SRC = Path(__file__).parent.parent.parent.parent / "autofix" / "cli" / "run_command.py"


def _body_lines() -> list[str]:
    """Return non-import, non-docstring lines of run_command.py."""
    text = _SRC.read_text(encoding="utf-8")
    out: list[str] = []
    in_module_docstring = False
    docstring_chars = ('"""', "'''")
    for raw in text.splitlines():
        stripped = raw.strip()
        if any(stripped.startswith(c) for c in docstring_chars):
            in_module_docstring = not in_module_docstring
            continue
        if in_module_docstring:
            continue
        # Skip import lines.
        if stripped.startswith("import ") or stripped.startswith("from "):
            continue
        out.append(raw)
    return out


def _body_text() -> str:
    return "\n".join(_body_lines())


def test_no_inline_evidence_placeholder() -> None:
    body = _body_text()
    # The literal `"0" * 64` form (or any 64-zero string) must NOT appear
    # outside the constants module.
    assert '"0" * 64' not in body
    assert "0" * 64 not in body


def test_no_inline_recovery_branch_prefix() -> None:
    body = _body_text()
    assert "autofix/pre-fix-snapshot-" not in body


def test_no_inline_timestamp_format() -> None:
    body = _body_text()
    assert "%Y%m%dT%H%M%SZ" not in body


def test_no_inline_threshold_literal() -> None:
    """The body MUST reference LLM_PATCH_THRESHOLD by name, not the literal 0.6."""
    body = _body_text()
    # `0.6` as a standalone literal — match `\b0\.6\b` to avoid matching
    # part of larger numbers.
    assert not re.search(r"(?<![0-9.])0\.6(?![0-9])", body), (
        "Inline 0.6 literal found; use LLM_PATCH_THRESHOLD"
    )


def test_no_inline_default_max_retries() -> None:
    """The body MUST reference DEFAULT_MAX_RETRIES, not the literal 3 in flag default."""
    body = _body_text()
    # `default=3` is the precise pattern we forbid (the constant should be passed instead).
    assert "default=3" not in body
