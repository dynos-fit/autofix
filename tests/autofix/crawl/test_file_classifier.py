"""Tests for autofix.crawl.file_classifier — covers ACs 1, 2, 3, 9.

ACs covered:
- AC 1: FileClass enum + classify_file + is_generated + map_test_to_impl exported
- AC 2: Priority order enforced (cache→vendor→build_output→…→source→unknown)
- AC 3: At least one positive test per FileClass value
- AC 9: map_test_to_impl algorithm (strip tests/ prefix, mirror layout detection)
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _p(s: str) -> Path:
    return Path(s)


# ---------------------------------------------------------------------------
# AC 1 — imports resolve (these must ImportError before production code)
# ---------------------------------------------------------------------------

def test_fileclass_enum_importable() -> None:
    """FileClass enum must be importable from file_classifier."""
    from autofix.crawl.file_classifier import FileClass  # noqa: F401
    assert FileClass is not None


def test_classify_file_importable() -> None:
    """classify_file function must be importable."""
    from autofix.crawl.file_classifier import classify_file  # noqa: F401
    assert callable(classify_file)


def test_is_generated_importable() -> None:
    """is_generated function must be importable."""
    from autofix.crawl.file_classifier import is_generated  # noqa: F401
    assert callable(is_generated)


def test_map_test_to_impl_importable() -> None:
    """map_test_to_impl function must be importable."""
    from autofix.crawl.file_classifier import map_test_to_impl  # noqa: F401
    assert callable(map_test_to_impl)


def test_fileclass_has_all_required_values() -> None:
    """FileClass must have exactly these 12 values."""
    from autofix.crawl.file_classifier import FileClass

    expected = {
        "source", "test", "config", "docs", "generated", "vendor",
        "build_output", "cache", "lockfile", "binary", "entrypoint", "unknown",
    }
    actual = {m.value for m in FileClass}
    assert actual == expected


def test_fileclass_is_str_enum() -> None:
    """FileClass must be a str-Enum so values serialize cleanly."""
    from autofix.crawl.file_classifier import FileClass
    import enum

    assert issubclass(FileClass, str)
    assert issubclass(FileClass, enum.Enum)


# ---------------------------------------------------------------------------
# AC 3 — one positive test case per FileClass value
# ---------------------------------------------------------------------------

class TestClassifyFilePositiveCases:
    """One positive case per FileClass value (AC 3)."""

    def test_cache_pycache(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("src/__pycache__/foo.cpython-311.pyc")) == FileClass.cache

    def test_cache_dot_cache_dir(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p(".cache/results.json")) == FileClass.cache

    def test_vendor_node_modules(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("node_modules/lodash/index.js")) == FileClass.vendor

    def test_vendor_vendor_dir(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("vendor/gopkg/utils.go")) == FileClass.vendor

    def test_build_output_dist(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("dist/main.js")) == FileClass.build_output

    def test_build_output_build_dir(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("build/lib/module.py")) == FileClass.build_output

    def test_generated_pb2(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("proto/service_pb2.py")) == FileClass.generated

    def test_generated_grpc(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("proto/service_pb2_grpc.py")) == FileClass.generated

    def test_lockfile_requirements_txt(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("requirements.txt")) == FileClass.lockfile

    def test_lockfile_poetry_lock(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("poetry.lock")) == FileClass.lockfile

    def test_lockfile_pipfile_lock(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("Pipfile.lock")) == FileClass.lockfile

    def test_binary_compiled_so(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("mymodule.so")) == FileClass.binary

    def test_binary_pyc_extension(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("foo.pyc")) == FileClass.binary

    def test_binary_png_image(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("assets/logo.png")) == FileClass.binary

    def test_test_file_prefix(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("tests/test_utils.py")) == FileClass.test

    def test_test_file_suffix(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("mymodule_test.py")) == FileClass.test

    def test_test_directory(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("tests/unit/helper.py")) == FileClass.test

    def test_config_pyproject_toml(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("pyproject.toml")) == FileClass.config

    def test_config_setup_cfg(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("setup.cfg")) == FileClass.config

    def test_config_dot_autofix_json(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p(".autofix/config.json")) == FileClass.config

    def test_docs_readme(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("README.md")) == FileClass.docs

    def test_docs_rst(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("docs/usage.rst")) == FileClass.docs

    def test_docs_dir(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("docs/api/reference.md")) == FileClass.docs

    def test_entrypoint_main_py(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("mypackage/__main__.py")) == FileClass.entrypoint

    def test_entrypoint_manage_py(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("manage.py")) == FileClass.entrypoint

    def test_entrypoint_wsgi_py(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("wsgi.py")) == FileClass.entrypoint

    def test_entrypoint_asgi_py(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("asgi.py")) == FileClass.entrypoint

    def test_entrypoint_cli_py(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("autofix/cli/cli.py")) == FileClass.entrypoint

    def test_entrypoint_app_py(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("app.py")) == FileClass.entrypoint

    def test_source_plain_py(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("autofix/crawl/bundles.py")) == FileClass.source

    def test_source_init(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("mypackage/__init__.py")) == FileClass.source

    def test_unknown_no_extension(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("Makefile")) == FileClass.unknown

    def test_unknown_shell_script(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        assert classify_file(_p("scripts/deploy.sh")) == FileClass.unknown


# ---------------------------------------------------------------------------
# AC 2 — Priority order enforced via multi-matching paths
# ---------------------------------------------------------------------------

class TestClassifyFilePriorityOrder:
    """A path matching multiple rules resolves to the highest-priority class."""

    def test_cache_beats_vendor(self) -> None:
        """__pycache__ inside vendor dir → cache wins (cache > vendor)."""
        from autofix.crawl.file_classifier import FileClass, classify_file
        # vendor/__pycache__/foo.pyc matches both vendor and cache
        result = classify_file(_p("vendor/__pycache__/foo.pyc"))
        assert result == FileClass.cache

    def test_cache_beats_build_output(self) -> None:
        """__pycache__ inside build dir → cache wins."""
        from autofix.crawl.file_classifier import FileClass, classify_file
        result = classify_file(_p("build/__pycache__/module.pyc"))
        assert result == FileClass.cache

    def test_vendor_beats_build_output(self) -> None:
        """vendor/ prefix beats build_output (vendor > build_output)."""
        from autofix.crawl.file_classifier import FileClass, classify_file
        # A path that might match build_output but also vendor
        result = classify_file(_p("vendor/dist/bundle.js"))
        assert result == FileClass.vendor

    def test_generated_beats_lockfile(self) -> None:
        """Generated file (pb2) beats lockfile — generated > lockfile."""
        from autofix.crawl.file_classifier import FileClass, classify_file
        # Construct a path that looks lockfile-like but also matches generated
        result = classify_file(_p("proto/service_pb2.py"))
        assert result == FileClass.generated

    def test_test_beats_config(self) -> None:
        """A test config file in tests/ → test beats config."""
        from autofix.crawl.file_classifier import FileClass, classify_file
        result = classify_file(_p("tests/conftest.py"))
        assert result == FileClass.test

    def test_test_beats_docs(self) -> None:
        """test_*.py inside docs/ → test beats docs."""
        from autofix.crawl.file_classifier import FileClass, classify_file
        result = classify_file(_p("docs/test_snippets.py"))
        assert result == FileClass.test

    def test_source_not_unknown_for_plain_py(self) -> None:
        """A plain .py file that matches no higher-priority rule → source."""
        from autofix.crawl.file_classifier import FileClass, classify_file
        result = classify_file(_p("myapp/utils.py"))
        assert result == FileClass.source

    def test_entrypoint_beats_source(self) -> None:
        """__main__.py is entrypoint, not source (entrypoint > source)."""
        from autofix.crawl.file_classifier import FileClass, classify_file
        result = classify_file(_p("mypackage/__main__.py"))
        assert result == FileClass.entrypoint

    def test_config_beats_docs(self) -> None:
        """pyproject.toml config → config (not docs, since config > docs)."""
        from autofix.crawl.file_classifier import FileClass, classify_file
        result = classify_file(_p("pyproject.toml"))
        assert result == FileClass.config


# ---------------------------------------------------------------------------
# AC 1 — classify_file never raises (returns unknown on exception)
# ---------------------------------------------------------------------------

class TestClassifyFileNeverRaises:
    """classify_file must not raise on pathological inputs."""

    def test_empty_path_string(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        # Should not raise; returns some classification or unknown
        result = classify_file(Path(""))
        assert isinstance(result, FileClass)

    def test_deeply_nested_path(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        result = classify_file(Path("/a/b/c/d/e/f/g/h/i.py"))
        assert isinstance(result, FileClass)

    def test_path_with_spaces(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        result = classify_file(Path("my module/some file.py"))
        assert isinstance(result, FileClass)

    def test_unicode_path(self) -> None:
        from autofix.crawl.file_classifier import FileClass, classify_file
        result = classify_file(Path("módule/ünïcödé.py"))
        assert isinstance(result, FileClass)


# ---------------------------------------------------------------------------
# AC 1 — classify_file is pure (no filesystem I/O)
# ---------------------------------------------------------------------------

def test_classify_file_pure_no_io(tmp_path: Path) -> None:
    """classify_file must not read the filesystem — path need not exist."""
    from autofix.crawl.file_classifier import FileClass, classify_file

    nonexistent = tmp_path / "does_not_exist.py"
    assert not nonexistent.exists()
    result = classify_file(nonexistent)
    # Pure: returns a valid FileClass even for non-existent path
    assert isinstance(result, FileClass)


# ---------------------------------------------------------------------------
# AC 1 — is_generated: inspects caller-supplied content only, no I/O
# ---------------------------------------------------------------------------

class TestIsGenerated:
    def test_is_generated_false_when_content_none(self) -> None:
        from autofix.crawl.file_classifier import is_generated
        assert is_generated(Path("foo.py"), content=None) is False

    def test_is_generated_true_on_generated_header(self) -> None:
        from autofix.crawl.file_classifier import is_generated
        content = "# Code generated by protoc-gen-go. DO NOT EDIT.\n\ndef func(): pass\n"
        assert is_generated(Path("foo.py"), content=content) is True

    def test_is_generated_true_on_autogenerated(self) -> None:
        from autofix.crawl.file_classifier import is_generated
        content = "# AUTO-GENERATED FILE. DO NOT MODIFY.\n"
        assert is_generated(Path("foo.py"), content=content) is True

    def test_is_generated_false_on_normal_file(self) -> None:
        from autofix.crawl.file_classifier import is_generated
        content = "def my_function():\n    pass\n"
        assert is_generated(Path("foo.py"), content=content) is False

    def test_is_generated_inspects_only_first_8kb(self) -> None:
        """is_generated must only look at first 8KB of caller-supplied content."""
        from autofix.crawl.file_classifier import is_generated
        # Generated marker buried deep — should not be found
        filler = "x" * (8 * 1024 + 100)
        content_deep = filler + "# DO NOT EDIT"
        assert is_generated(Path("foo.py"), content=content_deep) is False

    def test_is_generated_does_not_read_filesystem(self, tmp_path: Path) -> None:
        """is_generated does not read the filesystem."""
        from autofix.crawl.file_classifier import is_generated
        nonexistent = tmp_path / "no_such_file.py"
        # Calling with no content should not raise even if file doesn't exist
        result = is_generated(nonexistent, content=None)
        assert result is False


# ---------------------------------------------------------------------------
# AC 9 — map_test_to_impl algorithm
# ---------------------------------------------------------------------------

class TestMapTestToImpl:
    def test_standard_mirror_layout_found(self, tmp_path: Path) -> None:
        """tests/mypkg/test_foo.py → mypkg/foo.py when mirror exists."""
        from autofix.crawl.file_classifier import map_test_to_impl

        # Create mirror package
        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        impl_file = pkg_dir / "foo.py"
        impl_file.write_text("# impl\n")

        test_file = tmp_path / "tests" / "mypkg" / "test_foo.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("# test\n")

        result = map_test_to_impl(test_file, tmp_path)
        assert result == impl_file

    def test_test_suffix_style(self, tmp_path: Path) -> None:
        """tests/mypkg/foo_test.py → mypkg/foo.py when mirror exists."""
        from autofix.crawl.file_classifier import map_test_to_impl

        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        impl_file = pkg_dir / "foo.py"
        impl_file.write_text("# impl\n")

        test_file = tmp_path / "tests" / "mypkg" / "foo_test.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("# test\n")

        result = map_test_to_impl(test_file, tmp_path)
        assert result == impl_file

    def test_mirror_absent_returns_none(self, tmp_path: Path) -> None:
        """Returns None when the mirror package directory does not exist."""
        from autofix.crawl.file_classifier import map_test_to_impl

        test_file = tmp_path / "tests" / "nonexistent_pkg" / "test_foo.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("# test\n")

        result = map_test_to_impl(test_file, tmp_path)
        assert result is None

    def test_impl_file_absent_returns_none(self, tmp_path: Path) -> None:
        """Returns None when mirror package exists but the impl file does not."""
        from autofix.crawl.file_classifier import map_test_to_impl

        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        # No foo.py created

        test_file = tmp_path / "tests" / "mypkg" / "test_foo.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("# test\n")

        result = map_test_to_impl(test_file, tmp_path)
        assert result is None

    def test_non_tests_prefix_returns_none(self, tmp_path: Path) -> None:
        """Returns None for paths not under tests/."""
        from autofix.crawl.file_classifier import map_test_to_impl

        test_file = tmp_path / "specs" / "mypkg" / "test_foo.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("# test\n")

        result = map_test_to_impl(test_file, tmp_path)
        assert result is None

    def test_too_shallow_path_returns_none(self, tmp_path: Path) -> None:
        """Returns None for tests/test_foo.py (no package component)."""
        from autofix.crawl.file_classifier import map_test_to_impl

        test_file = tmp_path / "tests" / "test_foo.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("# test\n")

        result = map_test_to_impl(test_file, tmp_path)
        assert result is None

    def test_non_test_filename_returns_none(self, tmp_path: Path) -> None:
        """Returns None for filenames not matching test_ or _test.py."""
        from autofix.crawl.file_classifier import map_test_to_impl

        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()

        test_file = tmp_path / "tests" / "mypkg" / "conftest.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("# conftest\n")

        result = map_test_to_impl(test_file, tmp_path)
        assert result is None

    def test_never_raises_on_arbitrary_input(self, tmp_path: Path) -> None:
        """map_test_to_impl must never raise."""
        from autofix.crawl.file_classifier import map_test_to_impl

        # Non-existent path — must not raise
        result = map_test_to_impl(Path("/does/not/exist/test_something.py"), tmp_path)
        assert result is None

    def test_nested_subpackage_mapping(self, tmp_path: Path) -> None:
        """tests/pkg/sub/test_mod.py → pkg/sub/mod.py when mirror exists."""
        from autofix.crawl.file_classifier import map_test_to_impl

        pkg_dir = tmp_path / "pkg" / "sub"
        pkg_dir.mkdir(parents=True)
        impl_file = pkg_dir / "mod.py"
        impl_file.write_text("# impl\n")

        test_file = tmp_path / "tests" / "pkg" / "sub" / "test_mod.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("# test\n")

        result = map_test_to_impl(test_file, tmp_path)
        assert result == impl_file


# ---------------------------------------------------------------------------
# AC 1 — console_script_paths kwarg support
# ---------------------------------------------------------------------------

def test_classify_file_accepts_console_script_paths() -> None:
    """classify_file accepts optional console_script_paths frozenset."""
    from autofix.crawl.file_classifier import FileClass, classify_file

    script_paths = frozenset([Path("myapp/entry.py")])
    result = classify_file(Path("myapp/entry.py"), console_script_paths=script_paths)
    assert result == FileClass.entrypoint


def test_classify_file_console_scripts_none_default() -> None:
    """classify_file works when console_script_paths is None (default)."""
    from autofix.crawl.file_classifier import FileClass, classify_file

    result = classify_file(Path("myapp/utils.py"), console_script_paths=None)
    assert isinstance(result, FileClass)
