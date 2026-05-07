"""Integration tests for mypy adapter with real mypy binary.

These tests only run if mypy is installed on PATH.
They verify the adapter works correctly with actual mypy output.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Skip all tests in this module if mypy is not available
try:
    subprocess.run(
        ["mypy", "--version"],
        check=True,
        capture_output=True,
        timeout=5,
    )
except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
    pytestmark = pytest.mark.skip(reason="mypy not on PATH")


def test_real_assignment_error_in_tmp_path(tmp_path: Path) -> None:
    """Analyze a real Python file with assignment error using actual mypy.

    Writes a file with a type error, parses it with tree-sitter,
    and verifies mypy.analyze produces at least one assignment finding.
    """
    from autofix.analyzers.linter_passthrough import mypy
    from autofix.parsing.tree_sitter import parse_file

    # Write a Python file with assignment error
    py_file = tmp_path / "p.py"
    py_file.write_text('x: int = "hello"\n', encoding="utf-8")

    # Parse with tree-sitter
    parse_result = parse_file(py_file, repo_root=tmp_path)

    # Analyze with mypy
    findings = list(mypy.analyze(parse_result, symbol_table=None))

    # Should have at least one finding with assignment error
    assert len(findings) >= 1
    assignment_findings = [
        f for f in findings
        if f.rule_id == "linter:mypy:assignment"
    ]
    assert len(assignment_findings) >= 1
    assert assignment_findings[0].path == "p.py"
    assert assignment_findings[0].symbol_name == "assignment"


def test_real_clean_file(tmp_path: Path) -> None:
    """Analyze a clean Python file produces no findings.

    Verifies that a valid Python file with no issues produces
    an empty finding list.
    """
    from autofix.analyzers.linter_passthrough import mypy
    from autofix.parsing.tree_sitter import parse_file

    # Write a clean Python file
    py_file = tmp_path / "clean.py"
    py_file.write_text("x = 1\ny = x + 1\nprint(y)\n", encoding="utf-8")

    # Parse and analyze
    parse_result = parse_file(py_file, repo_root=tmp_path)
    findings = list(mypy.analyze(parse_result, symbol_table=None))

    # Should have no findings
    assert findings == []
