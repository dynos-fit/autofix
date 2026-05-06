"""TDD-first: assert the single autofix console script resolves correctly.

Covers:
- AC 9: pyproject.toml [project.scripts] has exactly one autofix entry.
- AC 12: `autofix --help` lists the 5 subcommands in the required order.

These tests MUST FAIL until task-20260506-003 lands.
"""
from __future__ import annotations

import io
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_scripts_single_autofix_entry() -> None:
    """AC 9: [project.scripts] contains exactly one entry: autofix = autofix.cli.main:main."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # find the [project.scripts] section
    m = re.search(
        r"\[project\.scripts\](.*?)(?=\n\[|\Z)",
        text,
        re.DOTALL,
    )
    assert m is not None, "[project.scripts] section missing"
    section = m.group(1)
    # count assignment lines (key = "...")
    lines = [
        ln.strip() for ln in section.strip().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert len(lines) == 1, (
        f"expected exactly 1 console script entry, got {len(lines)}: {lines!r}"
    )
    assert lines[0].startswith("autofix"), (
        f"expected entry to start with 'autofix', got {lines[0]!r}"
    )
    assert ("autofix" + "_next") not in lines[0], (
        f"console script still references autofix_next: {lines[0]!r}"
    )
    assert "autofix.cli.main:main" in lines[0], (
        f"console script does not point at autofix.cli.main:main: {lines[0]!r}"
    )


def test_autofix_cli_main_is_callable() -> None:
    """AC 12: autofix.cli.main.main is importable and callable."""
    from autofix.cli import main as main_module

    assert callable(main_module.main)


def test_autofix_help_lists_five_subcommands_in_order() -> None:
    """AC 12: autofix --help lists scan, replay, export-sarif, watch, policy in order."""
    from autofix.cli.main import main

    buf = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(err):
            main(["--help"])
    except SystemExit:
        pass
    output = buf.getvalue() + err.getvalue()
    expected_order = ["scan", "replay", "export-sarif", "watch", "policy"]
    positions = [output.find(name) for name in expected_order]
    assert all(p >= 0 for p in positions), (
        f"missing subcommands in --help output. positions={positions}, output={output!r}"
    )
    assert positions == sorted(positions), (
        f"subcommands not in expected order. positions={positions}, expected order={expected_order}"
    )
