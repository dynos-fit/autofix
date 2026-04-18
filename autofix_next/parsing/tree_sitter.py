"""Re-export shim for ``autofix_next.languages.python`` (task-006 AC #9).

The tree-sitter Python parser was moved to
:mod:`autofix_next.languages.python` in task-006 seg-2. This module is
retained as a thin forwarding shim so every task-001..005 import path
(``from autofix_next.parsing.tree_sitter import parse_file``, etc.)
continues to resolve unchanged.

Why this is non-trivial
-----------------------
Existing tests drive the parser cache invalidation by monkeypatching
``_load_language`` on ``autofix_next.parsing.tree_sitter``. The
underlying ``_ensure_parser`` in :mod:`autofix_next.languages.python`
reads ``_load_language`` via ``globals().get(...)`` on *its own* module,
so a naive ``from ... import _load_language`` re-export would not
propagate shim-side monkeypatches.

Strategy
--------
1. Static re-exports (``parse_file``, ``ParseResult``,
   ``TreeSitterLoadError``) satisfy AC #41 identity checks.
2. PEP 562 ``__getattr__`` lazily forwards every other name
   (``_load_language``, ``_language``, ``_parser``, ``_ensure_parser``,
   ``_cached_loader_id``, ``_installed_versions``, ``_raise_load_error``
   — anything tests might introspect) to the underlying module.
3. A module-class promotion to ``_ForwardingShim`` gives the shim a
   ``__setattr__`` that mirrors writes into
   :mod:`autofix_next.languages.python`. That means
   ``monkeypatch.setattr(ts_mod, "_load_language", fn)`` on the shim
   causes the underlying module's ``globals()["_load_language"]`` to
   flip, which in turn trips the ``id(loader)`` cache-invalidation
   check in ``_ensure_parser``.
"""

from __future__ import annotations

import sys as _sys
import types as _types

from autofix_next.languages import python as _py
from autofix_next.languages.python import (
    ParseResult,
    TreeSitterLoadError,
    parse_file,
)


def __getattr__(name):  # PEP 562 — lazy attribute lookup on the shim.
    """Forward any non-statically-re-exported attribute to ``_py``.

    Raises :class:`AttributeError` if the underlying module does not
    define ``name`` — matching the stock module-lookup contract.
    """
    try:
        return getattr(_py, name)
    except AttributeError as exc:  # pragma: no cover - pass-through
        raise AttributeError(
            f"module 'autofix_next.parsing.tree_sitter' has no attribute {name!r}"
        ) from exc


def __dir__():  # pragma: no cover - cosmetic
    return sorted(set(list(globals().keys()) + dir(_py)))


class _ForwardingShim(_types.ModuleType):
    """Module subclass whose writes mirror into the underlying module.

    ``monkeypatch.setattr(ts_mod, "_load_language", fn)`` needs the
    underlying module to observe the write so its ``_ensure_parser``
    cache-invalidation check (which compares ``id(_load_language)``
    against the cached loader id) sees the new function.
    """

    def __setattr__(self, name: str, value) -> None:  # type: ignore[override]
        # Mirror the write into the underlying module FIRST so the
        # cache-invalidation check inside ``_ensure_parser`` observes
        # the new binding; then set it on the shim so readers that
        # bypass ``__getattr__`` (e.g. ``ts_mod._load_language`` after
        # the attribute is materialized on the shim dict) see the same
        # object.
        setattr(_py, name, value)
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:  # type: ignore[override]
        # Mirror deletes too: tests may ``monkeypatch.delattr`` the
        # loader in the teardown path. Tolerate absence on either side.
        try:
            delattr(_py, name)
        except AttributeError:
            pass
        try:
            object.__delattr__(self, name)
        except AttributeError:
            pass


# Promote this module object to the ForwardingShim class so __setattr__
# and __delattr__ fire on module-attribute assignments. Must happen at
# the end of module initialization so the statically-bound names above
# are already set on the module dict.
_sys.modules[__name__].__class__ = _ForwardingShim


__all__ = [
    "TreeSitterLoadError",
    "ParseResult",
    "parse_file",
]
