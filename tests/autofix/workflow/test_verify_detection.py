"""Test runner detection (ARCH-011 AC-7)."""
from __future__ import annotations

from pathlib import Path


from autofix.workflow.verify import _detect_runner


def test_pyproject_marker_picks_pytest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    runner, command = _detect_runner(tmp_path)
    assert runner == "pytest"
    assert command == ("pytest", "-x")


def test_package_json_picks_jest(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"x"}')
    runner, command = _detect_runner(tmp_path)
    assert runner == "jest"
    assert command == ("npm", "test")


def test_go_mod_picks_go_test(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    runner, command = _detect_runner(tmp_path)
    assert runner == "go-test"
    assert command == ("go", "test", "./...")


def test_no_marker_returns_none_pair(tmp_path: Path) -> None:
    runner, command = _detect_runner(tmp_path)
    assert runner is None
    assert command is None


def test_priority_pyproject_over_package_json(tmp_path: Path) -> None:
    """When both Python and JS markers exist, Python wins."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "package.json").write_text('{"name":"x"}')
    runner, _ = _detect_runner(tmp_path)
    assert runner == "pytest"


def test_setup_py_also_picks_pytest(tmp_path: Path) -> None:
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    runner, _ = _detect_runner(tmp_path)
    assert runner == "pytest"
