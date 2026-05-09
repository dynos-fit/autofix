"""File classification for the continuous-crawl subsystem (task-20260508-002).

Pure path-based classification with a strict priority order so the
crawler can downrank junk (vendor, generated, build output, cache) and
favor real source. The classifier is deliberately I/O-free: callers
that need to inspect file *content* (e.g. ``is_generated``) pass the
content string in.

Priority order (highest -> lowest):

    cache -> vendor -> build_output -> generated -> lockfile ->
    binary -> test -> config -> docs -> entrypoint -> source -> unknown

Public surface:

* :class:`FileClass` - 12-value ``str``-Enum.
* :func:`classify_file` - pure classifier, never raises.
* :func:`is_generated` - inspects caller-supplied content (first 8 KiB).
* :func:`map_test_to_impl` - mirror-layout test->impl mapping.
* :func:`load_console_script_paths` - reads ``[project.scripts]`` once.
"""
from __future__ import annotations

import enum
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class FileClass(str, enum.Enum):
    """Classification of a path. Membership is closed; new values require
    an explicit spec update plus a constants/test refresh."""

    source = "source"
    test = "test"
    config = "config"
    docs = "docs"
    generated = "generated"
    vendor = "vendor"
    build_output = "build_output"
    cache = "cache"
    lockfile = "lockfile"
    binary = "binary"
    entrypoint = "entrypoint"
    unknown = "unknown"


# ---------------------------------------------------------------------------
# Internal classification rule sets (pure, frozen)
# ---------------------------------------------------------------------------

# Directory-component matchers -- a path is in this class iff *any*
# component of its parts() matches. Component matching avoids false
# hits on substrings like ``mybuild`` or ``vendored_doc``.

_CACHE_DIR_COMPONENTS: frozenset[str] = frozenset({
    "__pycache__",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
})

_VENDOR_DIR_COMPONENTS: frozenset[str] = frozenset({
    "node_modules",
    "vendor",
    "third_party",
    "third-party",
    "bower_components",
    ".venv",
    "venv",
    "site-packages",
})

_BUILD_OUTPUT_DIR_COMPONENTS: frozenset[str] = frozenset({
    "dist",
    "build",
    "out",
    "target",
    ".next",
    ".nuxt",
    "_build",
})

_TEST_DIR_COMPONENTS: frozenset[str] = frozenset({
    "tests",
    "test",
    "__tests__",
    "spec",
    "specs",
})

_DOCS_DIR_COMPONENTS: frozenset[str] = frozenset({
    "docs",
    "doc",
    "documentation",
})

# Binary file extensions (lowercase). The leading dot is included so
# we can match ``Path.suffix`` directly.
_BINARY_EXTS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".pyd",
    ".so", ".dylib", ".dll", ".a", ".o", ".lib",
    ".exe", ".bin", ".class", ".jar", ".war",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv", ".webm",
    ".db", ".sqlite", ".sqlite3",
})

# Lockfile filenames (exact match, case-sensitive on basename).
_LOCKFILE_NAMES: frozenset[str] = frozenset({
    "requirements.txt",
    "requirements-dev.txt",
    "requirements_dev.txt",
    "requirements-test.txt",
    "constraints.txt",
    "poetry.lock",
    "Pipfile.lock",
    "Pipfile",
    "uv.lock",
    "pdm.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
})

# Config filenames (exact match) and config extensions.
_CONFIG_NAMES: frozenset[str] = frozenset({
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "pytest.ini",
    ".flake8",
    ".pylintrc",
    "mypy.ini",
    "ruff.toml",
    ".ruff.toml",
    "package.json",
    "tsconfig.json",
    ".editorconfig",
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".env.example",
    "Makefile.in",
})

# Directory components that mark a file as config-by-location.
_CONFIG_DIR_COMPONENTS: frozenset[str] = frozenset({
    ".autofix",
    ".github",
    ".gitlab",
    ".circleci",
    ".vscode",
    ".idea",
})

# Documentation file extensions (lowercase).
_DOCS_EXTS: frozenset[str] = frozenset({
    ".md", ".markdown", ".rst", ".txt", ".adoc",
})

# Documentation filenames that should classify regardless of location.
_DOCS_NAMES: frozenset[str] = frozenset({
    "README", "README.md", "README.rst", "README.txt",
    "CHANGELOG", "CHANGELOG.md", "CHANGELOG.rst",
    "LICENSE", "LICENSE.md", "LICENSE.txt",
    "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
    "AUTHORS", "NOTICE",
})

# Entrypoint filenames (exact basename match).
_ENTRYPOINT_NAMES: frozenset[str] = frozenset({
    "__main__.py",
    "manage.py",
    "wsgi.py",
    "asgi.py",
    "cli.py",
    "app.py",
    "main.py",
    "server.py",
    "run.py",
})

# Source extensions -- everything else falls through to unknown.
_SOURCE_EXTS: frozenset[str] = frozenset({
    ".py", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".rb", ".java", ".kt", ".kts", ".scala",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh",
    ".cs", ".php", ".swift", ".m", ".mm",
})

# Generated-file content markers (case-insensitive substring search,
# anchored to first 8 KiB). Order doesn't matter; first hit wins.
_GENERATED_MARKERS: tuple[str, ...] = (
    "do not edit",
    "do not modify",
    "auto-generated",
    "autogenerated",
    "automatically generated",
    "code generated by",
    "@generated",
    "this file was generated",
    "this file is generated",
)

# Cap for is_generated() content scan (first 8 KiB only).
_IS_GENERATED_SCAN_BYTES: int = 8 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _components(path: Path) -> tuple[str, ...]:
    """Return the path components, lower-cased for cross-platform match.

    Pure: never touches the filesystem. ``Path.parts`` raises only on
    truly broken inputs (we already wrap callers in try/except).
    """
    return tuple(p for p in path.parts if p not in ("", "/", "\\"))


def _has_component(parts: tuple[str, ...], wanted: frozenset[str]) -> bool:
    """True iff any path component matches a wanted component."""
    return any(p in wanted for p in parts)


def _is_generated_filename(name: str) -> bool:
    """Generated files often follow ``*_pb2.py`` / ``*_pb2_grpc.py`` /
    ``*.pb.go`` / ``*.g.dart`` / ``*_generated.*`` / ``*.generated.*``
    naming conventions."""
    lower = name.lower()
    if lower.endswith("_pb2.py") or lower.endswith("_pb2_grpc.py"):
        return True
    if lower.endswith(".pb.go") or lower.endswith(".pb.cc") or lower.endswith(".pb.h"):
        return True
    if lower.endswith(".g.dart") or lower.endswith(".freezed.dart"):
        return True
    if "_generated." in lower or ".generated." in lower:
        return True
    if lower.endswith(".min.js") or lower.endswith(".min.css"):
        return True
    return False


def _is_test_filename(name: str) -> bool:
    """Test files match ``test_*.py`` / ``*_test.py`` / ``*.test.js`` /
    ``*.spec.ts`` etc."""
    lower = name.lower()
    if lower.startswith("test_") and lower.endswith(".py"):
        return True
    if lower.endswith("_test.py"):
        return True
    if lower == "conftest.py":
        return True
    # JS/TS conventions
    if any(lower.endswith(suf) for suf in (".test.js", ".test.ts", ".test.jsx", ".test.tsx",
                                            ".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx")):
        return True
    return False


# ---------------------------------------------------------------------------
# Public API: classify_file
# ---------------------------------------------------------------------------


def classify_file(
    path: Path,
    console_script_paths: Optional[frozenset[Path]] = None,
) -> FileClass:
    """Return the :class:`FileClass` for ``path``.

    Pure: no filesystem I/O. Never raises -- on any internal exception
    the function returns :attr:`FileClass.unknown` so a single
    pathological path cannot poison the crawler.

    Priority order is fixed:
        cache > vendor > build_output > generated > lockfile >
        binary > test > config > docs > entrypoint > source > unknown

    Parameters
    ----------
    path:
        The path to classify. Need not exist on disk.
    console_script_paths:
        Optional set of paths declared as ``[project.scripts]`` console
        entrypoints in ``pyproject.toml``. When provided, any path in
        the set is upgraded to :attr:`FileClass.entrypoint` (still
        below higher-priority classes).
    """
    try:
        parts = _components(path)
        name = path.name
        suffix = path.suffix.lower()

        # 1. cache -- highest priority. A pyc inside vendor/ is still
        # cache (test_cache_beats_vendor).
        if _has_component(parts, _CACHE_DIR_COMPONENTS):
            return FileClass.cache

        # 2. vendor
        if _has_component(parts, _VENDOR_DIR_COMPONENTS):
            return FileClass.vendor

        # 3. build_output
        if _has_component(parts, _BUILD_OUTPUT_DIR_COMPONENTS):
            return FileClass.build_output

        # 4. generated -- by filename convention only (content scan
        # lives in is_generated()).
        if _is_generated_filename(name):
            return FileClass.generated

        # 5. lockfile
        if name in _LOCKFILE_NAMES:
            return FileClass.lockfile

        # 6. binary -- by extension
        if suffix in _BINARY_EXTS:
            return FileClass.binary

        # 7. test -- filename pattern OR location under tests/
        if _is_test_filename(name):
            return FileClass.test
        if _has_component(parts, _TEST_DIR_COMPONENTS):
            return FileClass.test

        # 8. config -- exact name, extension, or config dir.
        if name in _CONFIG_NAMES:
            return FileClass.config
        if _has_component(parts, _CONFIG_DIR_COMPONENTS):
            return FileClass.config
        # ``.cfg`` / ``.ini`` / ``.toml`` / ``.yaml``/``.yml``/``.json``
        # files are typically configuration when they are not lockfiles
        # and not in vendor/build/cache (already filtered above).
        if suffix in {".cfg", ".ini", ".toml", ".yaml", ".yml"}:
            return FileClass.config

        # 9. docs -- by extension or by docs/ directory or by
        # well-known top-level filename.
        if name in _DOCS_NAMES:
            return FileClass.docs
        if _has_component(parts, _DOCS_DIR_COMPONENTS):
            return FileClass.docs
        if suffix in _DOCS_EXTS:
            return FileClass.docs

        # 10. entrypoint -- by filename or by console_scripts kwarg.
        if name in _ENTRYPOINT_NAMES:
            return FileClass.entrypoint
        if console_script_paths is not None and path in console_script_paths:
            return FileClass.entrypoint

        # 11. source
        if suffix in _SOURCE_EXTS:
            return FileClass.source

        # 12. unknown -- fallthrough
        return FileClass.unknown
    except Exception:  # noqa: BLE001 -- contract: never raise
        return FileClass.unknown


# ---------------------------------------------------------------------------
# Public API: is_generated
# ---------------------------------------------------------------------------


def is_generated(path: Path, content: Optional[str] = None) -> bool:
    """Return True iff ``content`` looks generated.

    Pure: never reads the filesystem. Inspects only the *first 8 KiB*
    of caller-supplied ``content`` -- markers buried deeper are
    ignored, mirroring the ``hashlib``-style scan budget used by the
    rest of the crawler.

    Parameters
    ----------
    path:
        Unused for the substring scan but accepted so callers can also
        consult filename heuristics via :func:`classify_file`.
    content:
        The file contents already read by the caller, or ``None``. When
        ``None``, the function returns ``False`` -- it deliberately
        does not open ``path`` itself.
    """
    try:
        if content is None:
            return False
        head = content[:_IS_GENERATED_SCAN_BYTES]
        head_lower = head.lower()
        for marker in _GENERATED_MARKERS:
            if marker in head_lower:
                return True
        return False
    except Exception:  # noqa: BLE001 -- contract: never raise
        return False


# ---------------------------------------------------------------------------
# Public API: map_test_to_impl
# ---------------------------------------------------------------------------


def map_test_to_impl(test_path: Path, root: Path) -> Optional[Path]:
    """Map a test file to its mirror implementation file.

    Algorithm:
        1. Path must be under ``tests/`` (i.e. parts[0] == 'tests').
        2. Strip the leading ``tests`` component.
        3. Verify the mirror package directory exists at
           ``root / parts[1]``.
        4. Strip the ``test_`` prefix or ``_test.py`` suffix from the
           filename. Filenames not matching either pattern -> None.
        5. The implementation file at the mirrored location must
           exist on disk; otherwise return None.

    Pure-by-contract for the path manipulation; uses ``Path.exists()``
    only to verify the mirror -- this *is* I/O, but it is bounded and
    the function never raises (any exception -> None).
    """
    try:
        # Resolve test_path relative to root so absolute fixture
        # paths under tmp_path work the same as repo-relative paths.
        try:
            rel = test_path.relative_to(root)
        except ValueError:
            # test_path was not under root -- fall back to using the
            # raw parts (covers callers that pass repo-relative paths).
            rel = test_path

        parts = rel.parts
        # parts[0] must be "tests" and we need at least
        # tests/<pkg>/<file> to have a mirror.
        if len(parts) < 3:
            return None
        if parts[0] != "tests":
            return None

        # Mirror package candidate: root / <pkg> (parts[1]).
        mirror_pkg = root / parts[1]
        if not mirror_pkg.exists() or not mirror_pkg.is_dir():
            return None

        # Strip the leading "tests" component, leaving e.g.
        # ("mypkg", "sub", "test_mod.py").
        rest = parts[1:]
        filename = rest[-1]
        subdirs = rest[:-1]  # includes the package itself

        # Filename remap: test_<x>.py -> <x>.py OR <x>_test.py -> <x>.py.
        if filename.startswith("test_") and filename.endswith(".py"):
            impl_name = filename[len("test_"):]
        elif filename.endswith("_test.py"):
            impl_name = filename[: -len("_test.py")] + ".py"
        else:
            return None

        impl_path = root.joinpath(*subdirs, impl_name)
        if not impl_path.exists():
            return None
        return impl_path
    except Exception:  # noqa: BLE001 -- contract: never raise
        return None


# ---------------------------------------------------------------------------
# Public API: load_console_script_paths
# ---------------------------------------------------------------------------


def load_console_script_paths(root: Path) -> frozenset[Path]:
    """Return the set of paths declared in ``[project.scripts]``.

    Reads ``root / 'pyproject.toml'`` once. Each ``"name = mod:func"``
    entry is mapped to the file ``mod.replace('.', '/') + '.py'``
    relative to ``root``. The function is robust to a missing
    pyproject, malformed TOML, or a pyproject without a
    ``[project.scripts]`` table -- in any of those cases it returns
    an empty frozenset rather than raising.
    """
    try:
        pyproject = root / "pyproject.toml"
        if not pyproject.is_file():
            return frozenset()

        # Python 3.11+ ships tomllib; fall back to tomli for 3.10.
        try:
            import tomllib  # type: ignore[import-not-found]
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[import-not-found,no-redef]
            except ImportError:
                return frozenset()

        with pyproject.open("rb") as f:
            data = tomllib.load(f)

        scripts = data.get("project", {}).get("scripts", {})
        if not isinstance(scripts, dict):
            return frozenset()

        paths: set[Path] = set()
        for _name, target in scripts.items():
            if not isinstance(target, str):
                continue
            mod = target.split(":", 1)[0].strip()
            if not mod:
                continue
            rel = Path(*mod.split(".")).with_suffix(".py")
            paths.add(rel)
        return frozenset(paths)
    except Exception:  # noqa: BLE001 -- contract: never raise
        return frozenset()


__all__ = [
    "FileClass",
    "classify_file",
    "is_generated",
    "map_test_to_impl",
    "load_console_script_paths",
]
