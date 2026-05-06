"""Tests for docs/rewrite/cli-retirement.md structure and content.

Covers:
- AC 15: file exists with three non-empty H2 sections.
- AC 16: Deprecation map has exactly 12 data rows, action values in
         {replace, rename, remove}, all 12 legacy commands present.
- AC 17: Retirement calendar contains T+0, T+30, T+90 milestones.
- AC 18: Operator migration steps contains 'autofix scan --root .'.
- AC 19 (C-02 meta-test): this file has >= 4 def test_ lines.

Parsing approach: read the file, split by '## ' to find sections, extract
pipe-delimited rows by splitting on '|', strip whitespace per cell.
NO external markdown library.

These tests MUST FAIL until docs/rewrite/cli-retirement.md is created.
"""
# seg-6 validated under task-20260506-002 (clean-slate-cli-cutover).

from __future__ import annotations

import inspect
from pathlib import Path


# ---------------------------------------------------------------------------
# Path to the doc under test
# ---------------------------------------------------------------------------

_DOC_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "rewrite" / "cli-retirement.md"
)

_EXPECTED_LEGACY_COMMANDS = {
    "autofix scan",
    "autofix list",
    "autofix clear",
    "autofix policy",
    "autofix sync-outcomes",
    "autofix benchmark",
    "autofix suppress",
    "autofix init",
    "autofix daemon",
    "autofix repo",
    "autofix config",
    "autofix scan-all",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_doc() -> str:
    """Read cli-retirement.md and return its content as a string."""
    assert _DOC_PATH.is_file(), (
        f"docs/rewrite/cli-retirement.md not found at {_DOC_PATH}. "
        "Production code for this task must be landed first."
    )
    return _DOC_PATH.read_text(encoding="utf-8")


def _get_section(content: str, section_title: str) -> str:
    """Extract text of an H2 section (from '## title' to next '## ' or EOF)."""
    marker = f"## {section_title}"
    start = content.find(marker)
    assert start != -1, (
        f"Section '## {section_title}' not found in cli-retirement.md"
    )
    # Find the next H2 heading after this one
    after = content.find("\n## ", start + len(marker))
    if after == -1:
        return content[start:]
    return content[start:after]


def _extract_table_rows(section_text: str) -> list[list[str]]:
    """Extract non-header, non-separator pipe-delimited rows from a section.

    Returns a list of rows, where each row is a list of cell strings
    (whitespace stripped per cell).
    """
    rows: list[list[str]] = []
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # Split on | and strip each cell
        cells = [cell.strip() for cell in line.split("|")]
        # Remove empty strings from the leading/trailing | splitting
        cells = [c for c in cells if c != ""]
        if not cells:
            continue
        # Skip the separator row (e.g. |---|---|---|---|)
        if all(set(c.replace("-", "").replace(":", "")) <= set() for c in cells):
            continue
        # Skip the separator row more robustly: all cells match /^[-:]+$/
        if all(all(ch in "-:" for ch in c) for c in cells):
            continue
        # Skip the header row (contains "legacy_command")
        if "legacy_command" in cells[0].lower():
            continue
        rows.append(cells)
    return rows


# ---------------------------------------------------------------------------
# AC 15: file exists with three non-empty H2 sections
# ---------------------------------------------------------------------------


def test_retirement_doc_exists() -> None:
    """AC 15: docs/rewrite/cli-retirement.md exists."""
    assert _DOC_PATH.is_file(), (
        f"Expected docs/rewrite/cli-retirement.md at {_DOC_PATH}"
    )


def test_deprecation_map_section_exists_and_nonempty() -> None:
    """AC 15: '## Deprecation map' section exists and is non-empty."""
    content = _read_doc()
    section = _get_section(content, "Deprecation map")
    body = section.replace("## Deprecation map", "").strip()
    assert body, "## Deprecation map section is empty"


def test_retirement_calendar_section_exists_and_nonempty() -> None:
    """AC 15: '## Retirement calendar' section exists and is non-empty."""
    content = _read_doc()
    section = _get_section(content, "Retirement calendar")
    body = section.replace("## Retirement calendar", "").strip()
    assert body, "## Retirement calendar section is empty"


def test_operator_migration_steps_section_exists_and_nonempty() -> None:
    """AC 15: '## Operator migration steps' section exists and is non-empty."""
    content = _read_doc()
    section = _get_section(content, "Operator migration steps")
    body = section.replace("## Operator migration steps", "").strip()
    assert body, "## Operator migration steps section is empty"


def test_sections_in_correct_order() -> None:
    """AC 15: three H2 sections appear in the required order."""
    content = _read_doc()
    pos_deprecation = content.find("## Deprecation map")
    pos_calendar = content.find("## Retirement calendar")
    pos_migration = content.find("## Operator migration steps")

    assert pos_deprecation != -1, "## Deprecation map not found"
    assert pos_calendar != -1, "## Retirement calendar not found"
    assert pos_migration != -1, "## Operator migration steps not found"

    assert pos_deprecation < pos_calendar < pos_migration, (
        f"Sections must appear in order: Deprecation map ({pos_deprecation}) < "
        f"Retirement calendar ({pos_calendar}) < "
        f"Operator migration steps ({pos_migration})"
    )


# ---------------------------------------------------------------------------
# AC 16: deprecation map — 12 data rows
# ---------------------------------------------------------------------------


def test_deprecation_map_has_twelve_rows() -> None:
    """AC 16: the Deprecation map table has exactly 12 data rows (one per
    legacy subcommand). Parses pipe-delimited rows, strips whitespace per cell,
    skips header and separator rows."""
    content = _read_doc()
    section = _get_section(content, "Deprecation map")
    rows = _extract_table_rows(section)
    assert len(rows) == 12, (
        f"expected 12 data rows in Deprecation map, got {len(rows)}: {rows!r}"
    )


# ---------------------------------------------------------------------------
# AC 16: deprecation map — action values
# ---------------------------------------------------------------------------


def test_deprecation_map_action_values() -> None:
    """AC 16: every row's 'action' cell (column index 1) is one of
    replace, rename, remove (case-insensitive)."""
    content = _read_doc()
    section = _get_section(content, "Deprecation map")
    rows = _extract_table_rows(section)

    valid_actions = {"replace", "rename", "remove"}
    for i, row in enumerate(rows):
        assert len(row) >= 2, (
            f"row {i} has fewer than 2 cells: {row!r}"
        )
        action = row[1].lower().strip("`").strip()
        assert action in valid_actions, (
            f"row {i} has invalid action {row[1]!r}; must be one of {valid_actions}"
        )


# ---------------------------------------------------------------------------
# AC 16: deprecation map — legacy command names
# ---------------------------------------------------------------------------


def test_deprecation_map_legacy_commands() -> None:
    """AC 16: all 12 expected legacy command names appear in the
    Deprecation map (first column, backtick-stripped, case-insensitive)."""
    content = _read_doc()
    section = _get_section(content, "Deprecation map")
    rows = _extract_table_rows(section)

    found_commands = set()
    for row in rows:
        if row:
            cmd = row[0].strip("`").strip()
            found_commands.add(cmd)

    for expected in _EXPECTED_LEGACY_COMMANDS:
        assert expected in found_commands, (
            f"Legacy command {expected!r} not found in Deprecation map. "
            f"Found: {sorted(found_commands)}"
        )


# ---------------------------------------------------------------------------
# AC 17: retirement calendar — T+0, T+30, T+90 milestones
# ---------------------------------------------------------------------------


def test_retirement_calendar_has_three_milestones() -> None:
    """AC 17: Retirement calendar section contains literals T+0, T+30, T+90."""
    content = _read_doc()
    section = _get_section(content, "Retirement calendar")

    for milestone in ("T+0", "T+30", "T+90"):
        assert milestone in section, (
            f"expected milestone {milestone!r} in '## Retirement calendar' section"
        )


def test_retirement_calendar_milestones_have_nonempty_bodies() -> None:
    """AC 17: each milestone T+0/T+30/T+90 is followed by non-empty content."""
    content = _read_doc()
    section = _get_section(content, "Retirement calendar")

    for milestone in ("T+0", "T+30", "T+90"):
        idx = section.find(milestone)
        assert idx != -1
        # Content after the milestone marker (at least something non-whitespace
        # on the same line or nearby lines)
        after = section[idx + len(milestone):idx + len(milestone) + 200]
        assert after.strip(), (
            f"milestone {milestone!r} has no body text following it"
        )


# ---------------------------------------------------------------------------
# AC 18: operator migration steps — autofix scan --root .
# ---------------------------------------------------------------------------


def test_operator_migration_steps_present() -> None:
    """AC 18: Operator migration steps section contains 'autofix scan --root .'."""
    content = _read_doc()
    section = _get_section(content, "Operator migration steps")

    assert "autofix scan --root ." in section, (
        f"expected 'autofix scan --root .' in '## Operator migration steps' section"
    )


# ---------------------------------------------------------------------------
# AC 19 + C-02: meta self-check test count
# ---------------------------------------------------------------------------


def test_meta_self_check_test_count() -> None:
    """AC 19 + C-02: this test file has >= 4 'def test_' lines (counts itself
    for AC 19 transparency and C-02 audit remediation)."""
    source = Path(__file__).read_text(encoding="utf-8")
    test_def_lines = [
        line for line in source.splitlines()
        if line.lstrip().startswith("def test_")
    ]
    count = len(test_def_lines)
    assert count >= 4, (
        f"expected >= 4 test functions in this file for AC 19 compliance, "
        f"found {count}"
    )
