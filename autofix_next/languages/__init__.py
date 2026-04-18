"""Language registry + ``LanguageAdapter`` Protocol (task-006 AC #1..#5).

Public surface
--------------
* :class:`LanguageAdapter` — ``@runtime_checkable`` Protocol that every
  language-specific parser adapter implements.
* :func:`register` — append an adapter to the registry (first-wins on
  duplicate ``language`` attribute; subsequent calls are silent no-ops).
* :func:`lookup_by_extension` — case-sensitive, leading-dot-form look-up
  by file extension (e.g. ``".py"`` → ``PythonAdapter`` instance).
* :func:`lookup_by_language` — look-up by the adapter's ``language``
  attribute (e.g. ``"python"``).
* :func:`all_adapters` — tuple of all registered adapters in registration
  order.
* :func:`all_extensions` — frozenset of every extension claimed by any
  registered adapter.

Adapters self-register at import time via the side-effect imports at the
bottom of this module. The import order is pinned to
``python -> jsts -> go`` so :func:`all_adapters` returns that exact
sequence. Sibling adapters (``jsts``, ``go``) land in later segments;
their imports are guarded with ``try/except ImportError`` so this
subpackage remains importable in the interim.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, TYPE_CHECKING, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - type-only
    from autofix_next.languages.python import ParseResult

_log = logging.getLogger(__name__)

# Registry internals. ``_REGISTRY`` preserves registration order for
# :func:`all_adapters`; ``_BY_LANGUAGE`` backs the O(1) first-wins check
# in :func:`register` and the :func:`lookup_by_language` fast path.
_REGISTRY: list["LanguageAdapter"] = []
_BY_LANGUAGE: dict[str, "LanguageAdapter"] = {}


@runtime_checkable
class LanguageAdapter(Protocol):
    """Protocol every language adapter must implement.

    Attributes
    ----------
    language:
        Canonical language name (e.g. ``"python"``, ``"typescript"``,
        ``"go"``). Used as the registry key: two adapters with the same
        ``language`` cannot both be registered — the first wins.
    extensions:
        Tuple of file extensions (leading-dot form) claimed by the
        adapter. Look-up via :func:`lookup_by_extension` is
        case-sensitive.

    Methods
    -------
    parse_cheap(source) -> ParseResult
        Fast parse suitable for scan-time cheap analyzers.
    parse_precise(source) -> ParseResult | None
        Optional precision parse. Return ``None`` when no precision
        pass beyond ``parse_cheap`` is available (Python today).
    symbol_kind(node) -> str
        Classify a parse-tree node into a symbol kind label.
    signature(node) -> str
        Produce a short signature string for a parse-tree node.
    scip_index(workdir) -> Path | None
        Produce (or reuse) a SCIP index artifact for a workdir. Return
        ``None`` when the adapter does not own a SCIP artifact itself
        (e.g. Python, where CallGraph drives the index).
    """

    language: str
    extensions: tuple[str, ...]

    def parse_cheap(self, source: bytes) -> "ParseResult": ...

    def parse_precise(self, source: bytes) -> "ParseResult | None": ...

    def symbol_kind(self, node) -> str: ...

    def signature(self, node) -> str: ...

    def scip_index(self, workdir) -> "Path | None": ...


def register(adapter: "LanguageAdapter") -> None:
    """Add ``adapter`` to the registry if its ``language`` is new.

    AC #4: duplicate ``language`` registrations are a silent no-op —
    the first adapter wins. The duplicate is logged at DEBUG level for
    operator visibility but never raised.
    """

    if adapter.language in _BY_LANGUAGE:
        _log.debug(
            "duplicate language registration for %r; keeping first-wins adapter",
            adapter.language,
        )
        return
    _REGISTRY.append(adapter)
    _BY_LANGUAGE[adapter.language] = adapter


def lookup_by_extension(ext: str) -> "LanguageAdapter | None":
    """Return the adapter claiming ``ext`` (exact, case-sensitive match).

    ``ext`` must be the leading-dot form (e.g. ``".py"``); ``"py"``
    does not match. Returns ``None`` when no adapter claims the
    extension.
    """

    for adapter in _REGISTRY:
        if ext in adapter.extensions:
            return adapter
    return None


def lookup_by_language(name: str) -> "LanguageAdapter | None":
    """Return the adapter whose ``language`` attribute equals ``name``.

    Returns ``None`` for unknown or empty names.
    """

    return _BY_LANGUAGE.get(name)


def all_adapters() -> tuple["LanguageAdapter", ...]:
    """Return every registered adapter in registration order (immutable)."""

    return tuple(_REGISTRY)


def all_extensions() -> frozenset[str]:
    """Return every extension claimed by any registered adapter."""

    return frozenset(ext for adapter in _REGISTRY for ext in adapter.extensions)


__all__ = [
    "LanguageAdapter",
    "register",
    "lookup_by_extension",
    "lookup_by_language",
    "all_adapters",
    "all_extensions",
]


# Side-effect imports for adapter self-registration (AC #5). The order
# is pinned ``python -> jsts -> go``. ``jsts`` and ``go`` land in later
# segments (seg-4, seg-5); until then their imports are guarded so the
# subpackage stays importable. Placed at module-end so ``register``,
# ``_REGISTRY``, and the ``LanguageAdapter`` Protocol are fully
# defined before any submodule runs its ``register(Adapter())`` call.
from autofix_next.languages import python as _python  # noqa: E402, F401

try:  # pragma: no cover - guarded until seg-4 lands jsts
    from autofix_next.languages import jsts as _jsts  # noqa: E402, F401
except ImportError:
    pass

try:  # pragma: no cover - guarded until seg-5 lands go
    from autofix_next.languages import go as _go  # noqa: E402, F401
except ImportError:
    pass
