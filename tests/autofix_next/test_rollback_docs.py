"""Tests for docs/rewrite/rollback.md structure.

Covers AC 16: the rollback doc exists with four non-empty sections in the
prescribed order, and section 3 contains the literal verification commands.

These tests do NOT depend on any production Python code; they inspect
the markdown file directly.  They will FAIL until docs/rewrite/rollback.md
is created by the docs segment.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLLBACK_DOC = REPO_ROOT / "docs" / "rewrite" / "rollback.md"

# Required section headers in order
REQUIRED_SECTIONS = [
    "How to disable autofix-next",
    "State guaranteed untouched",
    "Verification commands",
    "What is lost on rollback",
]

# Literal commands required in section 3
VERIFICATION_COMMANDS = [
    "autofix list --root .",
    "autofix policy --root .",
]


def test_rollback_doc_has_four_sections():
    # AC 16 — section headers
    """docs/rewrite/rollback.md must exist and contain all four required
    section headers in order, each followed by non-empty body text.
    """
    assert ROLLBACK_DOC.exists(), (
        f"docs/rewrite/rollback.md does not exist at {ROLLBACK_DOC}. "
        "Create it per AC 16."
    )

    content = ROLLBACK_DOC.read_text(encoding="utf-8")

    for section in REQUIRED_SECTIONS:
        assert section in content, (
            f"Required section '{section}' not found in {ROLLBACK_DOC}"
        )

    # Verify order: each section must appear after the previous one
    positions = [content.index(s) for s in REQUIRED_SECTIONS]
    for i in range(len(positions) - 1):
        assert positions[i] < positions[i + 1], (
            f"Section '{REQUIRED_SECTIONS[i]}' must appear before "
            f"'{REQUIRED_SECTIONS[i + 1]}' in {ROLLBACK_DOC}"
        )

    # Verify each section has non-empty body (something after the header before the next)
    lines = content.splitlines()
    for section in REQUIRED_SECTIONS:
        # Find the header line index
        header_idx = next(
            (i for i, line in enumerate(lines) if section in line),
            None,
        )
        assert header_idx is not None, f"Section '{section}' not found in lines"

        # Look for at least one non-empty, non-header line after this header
        # before the end of the file or the next section
        body_lines = []
        for line in lines[header_idx + 1:]:
            stripped = line.strip()
            # Stop at next section header (any of the required sections)
            if any(s in line for s in REQUIRED_SECTIONS):
                break
            if stripped:
                body_lines.append(stripped)

        assert body_lines, (
            f"Section '{section}' in {ROLLBACK_DOC} has no non-empty body text"
        )


def test_rollback_doc_contains_verification_commands():
    # AC 16 — literal verification commands
    """Section 'Verification commands' must contain both literal commands:
    'autofix list --root .' and 'autofix policy --root .'.
    """
    assert ROLLBACK_DOC.exists(), (
        f"docs/rewrite/rollback.md does not exist at {ROLLBACK_DOC}"
    )

    content = ROLLBACK_DOC.read_text(encoding="utf-8")

    for cmd in VERIFICATION_COMMANDS:
        assert cmd in content, (
            f"Literal command '{cmd}' not found in {ROLLBACK_DOC}. "
            "AC 16 requires this exact string in the Verification commands section."
        )
