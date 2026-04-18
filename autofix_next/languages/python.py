"""Tree-sitter Python parser wrapper (AC #21).

Responsibilities
----------------
1. Lazily load the tree-sitter Python grammar on first use, caching the
   resulting `Parser` at module scope so repeated parses do not re-pay the
   cost of grammar construction.
2. Normalize every failure mode of the underlying library — missing
   dependency, ABI mismatch, corrupt grammar binary — into a single
   :class:`TreeSitterLoadError` whose message names the installed
   tree-sitter / tree-sitter-python versions and the exact pip command a
   user must run to get back to a working state.
3. Offer `parse_file(path, repo_root=None)` that returns a self-contained
   :class:`ParseResult` holding the source bytes, the parsed tree, and the
   line-split source so downstream analyzers can extract slices without
   re-reading the file.

The two supported tree-sitter Python-binding API styles are handled:

* ``tree_sitter_python.language()`` returning a PyCapsule (0.21+ / 0.25+),
  wrapped with ``tree_sitter.Language(capsule)``.
* ``tree_sitter_python.language()`` returning a prebuilt ``Language``
  object directly (some interim releases).

Any ImportError at module import time is deferred: the module must remain
importable so other ``autofix_next`` subsystems can be introspected even
without the grammar installed. The error only surfaces at
``parse_file`` / ``_load_language`` invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Deferred imports: we must not hard-fail at module import time, because
# (a) production deps are wired in seg-5's pyproject and (b) the symbol
# table / analyzer test harness introspects attributes on this module
# before anyone calls parse_file.
#
# We use ``importlib.import_module`` rather than ``import tree_sitter``
# so AST-level import-graph scanners (notably
# ``autofix.platform.build_import_graph``) do not confuse the 3rd-party
# ``tree_sitter`` wheel with the in-repo ``autofix_next/parsing/tree_sitter.py``
# shim — their top-level stem collides and the legacy scanner resolves
# by stem. Using a dynamic import keeps the behavior byte-identical
# while suppressing the false-positive in-repo edge.
import importlib as _importlib

try:  # pragma: no cover - exercised only when deps are present
    _tree_sitter_mod = _importlib.import_module("tree_sitter")
    _TS_IMPORT_ERR: ImportError | None = None
except ImportError as _ts_import_err:  # pragma: no cover - exercised in stripped env
    _tree_sitter_mod = None  # type: ignore[assignment]
    _TS_IMPORT_ERR = _ts_import_err

try:  # pragma: no cover - exercised only when deps are present
    _tree_sitter_python_mod = _importlib.import_module("tree_sitter_python")
    _TSP_IMPORT_ERR: ImportError | None = None
except ImportError as _tsp_import_err:  # pragma: no cover
    _tree_sitter_python_mod = None  # type: ignore[assignment]
    _TSP_IMPORT_ERR = _tsp_import_err


class TreeSitterLoadError(RuntimeError):
    """Raised when the tree-sitter Python grammar cannot be loaded.

    Covers three categories of failure:

    * The ``tree_sitter`` or ``tree_sitter_python`` wheel is not installed.
    * An ABI mismatch between the runtime ``tree_sitter`` and the compiled
      grammar binary (e.g. "Incompatible Language version 14. Must be
      between 13 and 13").
    * Any other unexpected error surfaced by the grammar loader.

    The message always names the installed versions and the remediation
    command so support paths have enough information in a single
    stacktrace.
    """


def _installed_versions() -> tuple[str, str]:
    """Return ``(tree_sitter_version, tree_sitter_python_version)``.

    Both slots fall back to ``"not-installed"`` when the module is
    missing and ``"unknown"`` when the module is present but carries no
    ``__version__`` attribute. Pure introspection; never raises.
    """

    ts_ver = "not-installed"
    tsp_ver = "not-installed"
    if _tree_sitter_mod is not None:
        ts_ver = getattr(_tree_sitter_mod, "__version__", "unknown")
    if _tree_sitter_python_mod is not None:
        tsp_ver = getattr(_tree_sitter_python_mod, "__version__", "unknown")
    return ts_ver, tsp_ver


def _raise_load_error(cause: BaseException) -> None:
    """Raise a :class:`TreeSitterLoadError` with a fully-formed message."""

    ts_ver, tsp_ver = _installed_versions()
    raise TreeSitterLoadError(
        f"failed to load tree-sitter python grammar: {cause}. "
        f"Installed: tree-sitter={ts_ver}, tree-sitter-python={tsp_ver}. "
        "Fix: pip install 'tree-sitter>=0.21,<0.22' "
        "'tree-sitter-python>=0.21,<0.22'"
    ) from cause


# Module-level caches. ``_language`` holds the loaded Language object,
# ``_parser`` holds the Parser configured with that language, and
# ``_cached_loader_id`` records the ``id()`` of the ``_load_language``
# callable that built the cache. The id check lets tests invalidate the
# cache by ``monkeypatch.setattr(ts_mod, "_load_language", ...)`` — when
# the loader object changes, we rebuild.
_language: Any = None
_parser: Any = None
_cached_loader_id: int | None = None


def _load_language() -> Any:
    """Return a tree-sitter ``Language`` object for Python.

    This helper is named so that tests can monkeypatch it to simulate
    ABI-mismatch failures (see ``test_parsing_tree_sitter.py``). Any
    exception raised here — including synthetic ones injected by tests
    via ``monkeypatch.setattr`` — is caught by ``_ensure_parser`` and
    re-raised as :class:`TreeSitterLoadError`.
    """

    if _tree_sitter_mod is None:
        raise _TS_IMPORT_ERR if _TS_IMPORT_ERR is not None else ImportError(
            "tree_sitter is not installed"
        )
    if _tree_sitter_python_mod is None:
        raise _TSP_IMPORT_ERR if _TSP_IMPORT_ERR is not None else ImportError(
            "tree_sitter_python is not installed"
        )

    # Preferred path (0.21+ canonical): ``tree_sitter_python.language()``
    # returns either a PyCapsule to wrap in ``Language(...)`` or, in a
    # handful of interim releases, a ready-built ``Language``. Accept both.
    raw = _tree_sitter_python_mod.language()
    language_cls = getattr(_tree_sitter_mod, "Language")
    if isinstance(raw, language_cls):
        return raw
    return language_cls(raw)


def _ensure_parser() -> Any:
    """Return the cached ``Parser``, building it on first call.

    All failure paths — including monkeypatched ``_load_language`` that
    raises arbitrary exceptions — are normalized into
    :class:`TreeSitterLoadError`.
    """

    global _language, _parser, _cached_loader_id

    # Look up the loader via the module globals so that monkeypatching
    # ``_load_language`` on this module (as tests do) takes effect here.
    # A bare ``_load_language()`` call would bind at definition time in
    # some patching styles.
    loader = globals().get("_load_language")
    if loader is None:  # pragma: no cover - defensive
        _raise_load_error(RuntimeError("_load_language missing from module"))

    # Invalidate the cached parser when the loader has been swapped out
    # (monkeypatching in tests, or a hot-reload). Identity comparison is
    # intentional: we want a replaced callable — even one with the same
    # source text — to force a rebuild so the new behavior takes effect.
    if _parser is not None and _cached_loader_id == id(loader):
        return _parser
    _parser = None
    _language = None

    try:
        language = loader()
    except TreeSitterLoadError:
        raise
    except BaseException as exc:  # noqa: BLE001 — deliberately broad
        _raise_load_error(exc)

    if _tree_sitter_mod is None:  # pragma: no cover - defensive
        _raise_load_error(RuntimeError("tree_sitter module disappeared"))

    parser_cls = getattr(_tree_sitter_mod, "Parser")
    try:
        # tree-sitter >= 0.22 accepts ``Parser(language)`` directly; older
        # 0.21 releases use the ``Parser().set_language(language)`` two-step
        # API. Try the new constructor first, fall back cleanly.
        try:
            parser = parser_cls(language)
        except TypeError:
            parser = parser_cls()
            parser.set_language(language)
    except BaseException as exc:  # noqa: BLE001
        _raise_load_error(exc)

    _language = language
    _parser = parser
    _cached_loader_id = id(loader)
    return _parser


@dataclass(slots=True)
class ParseResult:
    """Output of a single-file parse.

    Attributes
    ----------
    path:
        Absolute filesystem path that was parsed.
    relpath:
        Path relative to the repo root supplied to :func:`parse_file`.
        Falls back to the file's basename when no root is given. This is
        the path used to build stable ``finding_id`` fingerprints, so it
        must be deterministic across machines.
    source_bytes:
        The exact UTF-8 bytes read from disk. Retained so downstream
        slice extraction matches tree-sitter's byte offsets.
    tree:
        The ``tree_sitter.Tree`` produced by the Python grammar. Typed as
        ``Any`` because the concrete class is not importable at module
        load time (the dep may be absent).
    lines:
        ``source_bytes.decode("utf-8").split("\\n")`` — pre-split to let
        analyzers produce ``changed_slice`` excerpts without re-splitting
        on every finding.
    """

    path: Path
    relpath: str
    source_bytes: bytes
    tree: Any
    lines: list[str]


def parse_file(path: Path, repo_root: Path | None = None) -> ParseResult:
    """Parse a single Python source file.

    Parameters
    ----------
    path:
        File to parse. Must exist and be UTF-8 decodable.
    repo_root:
        Optional repo root used to compute ``relpath``. When absent,
        ``path.name`` is used instead.

    Returns
    -------
    ParseResult

    Raises
    ------
    TreeSitterLoadError
        The tree-sitter grammar could not be loaded. Message includes
        installed versions and remediation command.
    FileNotFoundError
        The target file does not exist.
    OSError
        Any lower-level IO error surfaces unwrapped (permission denied,
        IsADirectoryError, etc.) — callers are responsible for deciding
        whether such errors are fatal.
    """

    parser = _ensure_parser()

    try:
        source_bytes = path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError:
        raise

    try:
        tree = parser.parse(source_bytes)
    except BaseException as exc:  # noqa: BLE001 — grammar can surface odd errors
        _raise_load_error(exc)

    if repo_root is not None:
        try:
            relpath = str(path.resolve().relative_to(repo_root.resolve()))
        except ValueError:
            # Path is outside repo_root — fall back to an absolute path so
            # the fingerprint stays unique rather than collapsing to name.
            relpath = str(path)
    else:
        relpath = path.name

    try:
        text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Non-UTF-8 source is rare in Python 3 repos but must surface with
        # a clean error rather than a raw decode traceback.
        raise TreeSitterLoadError(
            f"source file {path} is not valid UTF-8: {exc}"
        ) from exc
    lines = text.split("\n")

    return ParseResult(
        path=path,
        relpath=relpath,
        source_bytes=source_bytes,
        tree=tree,
        lines=lines,
    )


class PythonAdapter:
    """LanguageAdapter implementation for Python sources (task-006 AC #7).

    The Python path keeps ``parse_file`` as its canonical entrypoint —
    the funnel pipeline routes ``.py`` inputs through ``parse_file``
    directly — so ``parse_cheap`` here is a minimal wrapper provided for
    Protocol conformance. ``parse_precise`` returns ``None``: precision
    for Python is supplied by the SCIP index (built via
    :mod:`autofix_next.indexing.call_graph`) rather than a second
    tree-sitter parse.
    """

    language: str = "python"
    extensions: tuple[str, ...] = (".py",)

    def parse_cheap(self, source: bytes) -> ParseResult:
        """Parse ``source`` bytes and return a :class:`ParseResult`.

        No filesystem read happens here: ``path`` is set to an empty
        in-memory sentinel (``Path("<memory>")``) and ``relpath`` to
        ``"<memory>"`` because this entry point is byte-oriented. The
        funnel pipeline does not invoke this method for Python today —
        it uses :func:`parse_file` — so the in-memory placeholder is
        acceptable for Protocol conformance.
        """

        parser = _ensure_parser()
        try:
            tree = parser.parse(source)
        except BaseException as exc:  # noqa: BLE001 — grammar can surface odd errors
            _raise_load_error(exc)

        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TreeSitterLoadError(
                f"in-memory source is not valid UTF-8: {exc}"
            ) from exc
        lines = text.split("\n")

        return ParseResult(
            path=Path("<memory>"),
            relpath="<memory>",
            source_bytes=source,
            tree=tree,
            lines=lines,
        )

    def parse_precise(self, source: bytes) -> ParseResult | None:
        """Precision parse is not implemented for Python in task-006.

        Python precision is supplied by the SCIP index (see
        :mod:`autofix_next.indexing.call_graph`) rather than a second
        tree-sitter pass. Returning ``None`` signals "no additional
        precision available beyond ``parse_cheap``" to the scheduler.
        """

        return None

    def symbol_kind(self, node) -> str:
        """Return the tree-sitter node type as the symbol kind label.

        No test exercises this method today; Python symbol classification
        is handled inside :mod:`autofix_next.indexing.symbols`. The
        simple passthrough keeps the Protocol contract satisfied.
        """

        return getattr(node, "type", "unknown")

    def signature(self, node) -> str:
        """Return a short signature string for ``node``.

        Placeholder implementation (see ``symbol_kind`` note); callers
        wanting true Python signatures should consult the SCIP index.
        """

        return getattr(node, "type", "")

    def scip_index(self, workdir) -> Path | None:
        """Python SCIP indexing is driven by ``CallGraph.build_from_root``.

        The adapter path returns ``None`` to signal "no adapter-owned
        SCIP artifact". The scheduler (task-005 funnel) already routes
        Python precision through the CallGraph path; exposing that here
        would duplicate the pipeline wiring.
        """

        return None


__all__ = [
    "TreeSitterLoadError",
    "ParseResult",
    "parse_file",
    "PythonAdapter",
]


# Self-registration (AC #8): invoked exactly once per process, at the
# moment ``autofix_next.languages`` imports this submodule. We import
# ``register`` lazily inside the call to avoid a circular import at
# class-definition time — the parent package defines ``register``
# BEFORE importing this module, so the lookup succeeds.
from autofix_next.languages import register as _register  # noqa: E402

_register(PythonAdapter())
