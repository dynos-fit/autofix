"""Integration tests for ruff adapter with real ruff binary.

These tests only run if ruff is installed on PATH.
They verify the adapter works correctly with actual ruff output.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Skip all tests in this module if ruff is not available
try:
    subprocess.run(
        ["ruff", "--version"],
        check=True,
        capture_output=True,
        timeout=5,
    )
except (FileNotFoundError, subprocess.CalledProcessError):
    pytestmark = pytest.mark.skip(reason="ruff not on PATH")


def test_real_f401_in_tmp_path(tmp_path: Path) -> None:
    """Analyze a real Python file with unused import using actual ruff.

    Writes a file with an unused import, parses it with tree-sitter,
    and verifies ruff.analyze produces exactly one F401 finding.
    """
    from autofix.analyzers.linter_passthrough import ruff
    from autofix.parsing.tree_sitter import parse_file

    # Write a Python file with unused import
    py_file = tmp_path / "p.py"
    py_file.write_text("import os\n\nx = 1\n", encoding="utf-8")

    # Parse with tree-sitter
    parse_result = parse_file(py_file, repo_root=tmp_path)

    # Analyze with ruff
    findings = list(ruff.analyze(parse_result, symbol_table=None))

    # Should have exactly one F401 finding
    assert len(findings) == 1
    assert findings[0].rule_id == "linter:ruff:F401"
    assert findings[0].path == "p.py"
    assert findings[0].symbol_name == "F401"
    assert findings[0].start_line >= 1


def test_real_clean_file(tmp_path: Path) -> None:
    """Analyze a clean Python file produces no findings.

    Verifies that a valid Python file with no issues produces
    an empty finding list.
    """
    from autofix.analyzers.linter_passthrough import ruff
    from autofix.parsing.tree_sitter import parse_file

    # Write a clean Python file
    py_file = tmp_path / "clean.py"
    py_file.write_text("import os\n\npath = os.getcwd()\n", encoding="utf-8")

    # Parse and analyze
    parse_result = parse_file(py_file, repo_root=tmp_path)
    findings = list(ruff.analyze(parse_result, symbol_table=None))

    # Should have no findings
    assert findings == []


def test_real_multiple_issues(tmp_path: Path) -> None:
    """Analyze a file with multiple ruff violations.

    Verifies that multiple violations are all captured.
    """
    from autofix.analyzers.linter_passthrough import ruff
    from autofix.parsing.tree_sitter import parse_file

    # Write a file with multiple issues (unused import + unused variable)
    py_file = tmp_path / "issues.py"
    py_file.write_text(
        "import os\nimport sys\n\nunused_var = 1\nx = 2\n",
        encoding="utf-8",
    )

    parse_result = parse_file(py_file, repo_root=tmp_path)
    findings = list(ruff.analyze(parse_result, symbol_table=None))

    # Should have at least 2 findings (unused imports/variables)
    assert len(findings) >= 2
    # All should be from linter:ruff
    assert all(f.rule_id.startswith("linter:ruff:") for f in findings)


def test_real_nested_path(tmp_path: Path) -> None:
    """Analyze a file in a nested directory structure.

    Verifies that relative paths are correctly preserved
    when the file is in a subdirectory.
    """
    from autofix.analyzers.linter_passthrough import ruff
    from autofix.parsing.tree_sitter import parse_file

    # Create nested directory structure
    subdir = tmp_path / "src" / "module"
    subdir.mkdir(parents=True)
    py_file = subdir / "deep.py"
    py_file.write_text("import json\n\nx = 1\n", encoding="utf-8")

    parse_result = parse_file(py_file, repo_root=tmp_path)
    findings = list(ruff.analyze(parse_result, symbol_table=None))

    # Should have one F401 for unused json import
    assert len(findings) == 1
    # The relative path should be preserved
    assert findings[0].path == "src/module/deep.py"
    assert findings[0].rule_id == "linter:ruff:F401"
