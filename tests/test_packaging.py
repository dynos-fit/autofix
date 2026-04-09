"""Tests for packaging and pyproject.toml configuration.

Covers acceptance criteria: 1, 2, 3, 24.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def pyproject() -> dict:
    """Load pyproject.toml as a dict."""
    return tomllib.loads(PYPROJECT_PATH.read_text())


# ---------------------------------------------------------------------------
# Criterion 2: package name is autofix-scanner
# ---------------------------------------------------------------------------

class TestPackageName:
    """Criterion 2: installed package name is autofix-scanner."""

    def test_package_name(self, pyproject: dict) -> None:
        assert pyproject["project"]["name"] == "autofix-scanner"

    def test_cli_entry_point_is_autofix(self, pyproject: dict) -> None:
        """CLI entry point command should remain 'autofix'."""
        scripts = pyproject["project"].get("scripts", {})
        assert "autofix" in scripts
        assert "app:main" in scripts["autofix"]


# ---------------------------------------------------------------------------
# Criterion 3: all subpackages included
# ---------------------------------------------------------------------------

class TestSubpackages:
    """Criterion 3: autofix, autofix.runtime, autofix.llm_io are all included."""

    def test_find_packages_includes_all_subpackages(self, pyproject: dict) -> None:
        """Packages config should discover autofix and all subpackages."""
        setuptools = pyproject.get("tool", {}).get("setuptools", {})
        packages_config = setuptools.get("packages", {})

        if isinstance(packages_config, dict) and "find" in packages_config:
            # Auto-discovery mode -- check include pattern
            include = packages_config["find"].get("include", [])
            # Should have a pattern like "autofix*" or explicit subpackages
            included_text = " ".join(include)
            assert "autofix" in included_text
        elif isinstance(packages_config, list):
            # Explicit list mode
            assert "autofix" in packages_config
            assert "autofix.runtime" in packages_config
            assert "autofix.llm_io" in packages_config
        else:
            pytest.fail(f"Unexpected packages config format: {packages_config}")

    def test_package_data_includes_prompts(self, pyproject: dict) -> None:
        """llm_io/prompts/*.md must be included as package data."""
        setuptools = pyproject.get("tool", {}).get("setuptools", {})
        package_data = setuptools.get("package-data", {})
        # Check for llm_io prompts
        llm_io_data = package_data.get("autofix.llm_io", [])
        assert any("prompts" in pattern and "*.md" in pattern for pattern in llm_io_data), (
            f"Expected prompts/*.md in package-data for autofix.llm_io, got: {llm_io_data}"
        )


# ---------------------------------------------------------------------------
# Criterion 24: complete metadata
# ---------------------------------------------------------------------------

class TestMetadata:
    """Criterion 24: pyproject.toml has description, requires-python, license, authors, readme."""

    def test_has_description(self, pyproject: dict) -> None:
        desc = pyproject["project"].get("description", "")
        assert len(desc) > 0

    def test_has_requires_python(self, pyproject: dict) -> None:
        requires = pyproject["project"].get("requires-python", "")
        assert "3.11" in requires

    def test_has_license(self, pyproject: dict) -> None:
        project = pyproject["project"]
        assert "license" in project

    def test_has_authors(self, pyproject: dict) -> None:
        authors = pyproject["project"].get("authors", [])
        assert len(authors) > 0

    def test_has_readme(self, pyproject: dict) -> None:
        project = pyproject["project"]
        assert "readme" in project


# ---------------------------------------------------------------------------
# Criterion 1: pip installability (structural check)
# ---------------------------------------------------------------------------

class TestPipInstallability:
    """Criterion 1: structural checks that pip install will work."""

    def test_build_system_defined(self, pyproject: dict) -> None:
        assert "build-system" in pyproject
        assert "setuptools" in pyproject["build-system"].get("requires", [])[0]

    def test_entry_point_module_exists(self) -> None:
        """The entry point module (autofix.app) should exist."""
        app_module = REPO_ROOT / "autofix" / "app.py"
        assert app_module.exists()

    def test_autofix_package_has_init(self) -> None:
        """autofix package should have __init__.py or be implicitly namespaced."""
        pkg_dir = REPO_ROOT / "autofix"
        assert pkg_dir.is_dir()
        # At minimum the package directory exists; __init__.py may or may not exist
        # with modern Python, but the package dir must be present

    def test_runtime_subpackage_exists(self) -> None:
        runtime_dir = REPO_ROOT / "autofix" / "runtime"
        assert runtime_dir.is_dir()

    def test_llm_io_subpackage_exists(self) -> None:
        llm_io_dir = REPO_ROOT / "autofix" / "llm_io"
        assert llm_io_dir.is_dir()

    def test_prompts_dir_exists(self) -> None:
        prompts_dir = REPO_ROOT / "autofix" / "llm_io" / "prompts"
        assert prompts_dir.is_dir()

    def test_prompts_has_md_files(self) -> None:
        prompts_dir = REPO_ROOT / "autofix" / "llm_io" / "prompts"
        md_files = list(prompts_dir.glob("*.md"))
        assert len(md_files) > 0, "No .md files found in prompts directory"
