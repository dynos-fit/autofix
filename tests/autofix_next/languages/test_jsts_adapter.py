"""Tests for ``autofix_next.languages.jsts.JSTSAdapter`` (task-006 AC
#12, #13, #14).

Coverage
--------
* AC #12 — ``extensions == ('.ts', '.tsx', '.js', '.jsx')``, language
  == 'typescript', ``available: bool`` attribute set at registration.
* AC #12 — grammar-absent path: ``available=False`` + ``parse_cheap``
  returns an empty-tree ``ParseResult``-shaped object without raising.
* AC #13 — ``scip_index(workdir)`` method signature exists (actual
  subprocess invocation is marked ``@requires_scip_binaries`` so CI
  without the binaries skips; see ``conftest.py``).
"""

from __future__ import annotations

import inspect
import sys
from typing import Any

import pytest


def _import_jsts():
    return pytest.importorskip("autofix_next.languages.jsts")


# ---------------------------------------------------------------------------
# AC #12 — extensions + language
# ---------------------------------------------------------------------------


def test_jsts_adapter_extensions_and_language() -> None:
    """AC #12: language == 'typescript', extensions == ('.ts','.tsx','.js','.jsx')."""
    jsts_mod = _import_jsts()
    JSTSAdapter = jsts_mod.JSTSAdapter
    adapter = JSTSAdapter()
    assert adapter.language == "typescript", (
        f"JSTSAdapter.language must be 'typescript', got {adapter.language!r}"
    )
    assert adapter.extensions == (".ts", ".tsx", ".js", ".jsx"), (
        f"JSTSAdapter.extensions must be ('.ts','.tsx','.js','.jsx'), "
        f"got {adapter.extensions!r}"
    )


# ---------------------------------------------------------------------------
# AC #12 — available flag set at registration time
# ---------------------------------------------------------------------------


def test_jsts_adapter_available_flag_set_at_registration() -> None:
    """AC #12: every registered ``JSTSAdapter`` has an ``available: bool``
    attribute whose value reflects the result of the grammar-import probe
    performed at registration time.
    """
    jsts_mod = _import_jsts()
    JSTSAdapter = jsts_mod.JSTSAdapter
    adapter = JSTSAdapter()
    assert hasattr(adapter, "available"), (
        "JSTSAdapter must expose an ``available: bool`` attribute"
    )
    assert isinstance(adapter.available, bool), (
        f"JSTSAdapter.available must be bool, got {type(adapter.available).__name__}"
    )


# ---------------------------------------------------------------------------
# AC #12 — grammar-absent path
# ---------------------------------------------------------------------------


def test_jsts_adapter_parse_cheap_empty_tree_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #12: if the tree-sitter-typescript grammar import fails,
    ``JSTSAdapter`` constructs with ``available=False`` and
    ``parse_cheap(source)`` returns an empty-tree ``ParseResult``-shaped
    object without raising.

    We simulate the grammar-missing condition by removing
    ``tree_sitter_typescript`` from ``sys.modules`` and blocking its
    re-import for the duration of the test.
    """
    jsts_mod = _import_jsts()

    # Force the grammar module to look missing for any fresh construction.
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tree_sitter_typescript" or name.startswith(
            "tree_sitter_typescript."
        ):
            raise ImportError("simulated missing tree_sitter_typescript")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "tree_sitter_typescript", raising=False)
    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    JSTSAdapter = jsts_mod.JSTSAdapter
    # Fresh construction must re-probe the grammar and land available=False.
    adapter = JSTSAdapter()
    assert adapter.available is False, (
        "with tree_sitter_typescript missing, JSTSAdapter.available must be False"
    )

    # parse_cheap must not raise; must return a ParseResult-shaped object
    # with an empty (None-tree-ok) field so downstream side-effect-only
    # callers can ignore it.
    source = b"const x = 1;\n"
    result = adapter.parse_cheap(source)
    assert result is not None, (
        "parse_cheap must return a ParseResult-shaped object even when "
        "grammar is missing, not None"
    )
    # The result must carry the original source bytes so callers can
    # introspect without re-reading the file.
    assert hasattr(result, "source_bytes"), (
        f"parse_cheap result must expose source_bytes, got {dir(result)!r}"
    )
    assert bytes(result.source_bytes) == source
    # Tree may be None or an empty tree; both are acceptable.
    assert hasattr(result, "tree"), (
        "parse_cheap result must expose a .tree attribute (may be None)"
    )


# ---------------------------------------------------------------------------
# AC #13 — scip_index signature
# ---------------------------------------------------------------------------


def test_jsts_adapter_scip_index_signature() -> None:
    """AC #13: ``JSTSAdapter.scip_index`` is callable; accepts at least a
    ``workdir`` positional/keyword parameter; returns ``Path | None``
    (the runtime check is a signature-inspection smoke check only —
    actual subprocess invocation is guarded by
    ``@pytest.mark.requires_scip_binaries``).
    """
    jsts_mod = _import_jsts()
    JSTSAdapter = jsts_mod.JSTSAdapter
    adapter = JSTSAdapter()
    assert callable(adapter.scip_index), (
        "JSTSAdapter.scip_index must be callable"
    )
    sig = inspect.signature(adapter.scip_index)
    param_names = list(sig.parameters.keys())
    assert "workdir" in param_names, (
        f"JSTSAdapter.scip_index must take a workdir param, got {param_names!r}"
    )


def test_jsts_adapter_parse_precise_returns_none() -> None:
    """AC #12 (derived): ``parse_precise`` returns ``None`` today —
    precision for JS/TS is delivered via ``scip_index``, not
    ``parse_precise``."""
    jsts_mod = _import_jsts()
    JSTSAdapter = jsts_mod.JSTSAdapter
    adapter = JSTSAdapter()
    assert adapter.parse_precise(b"const x = 1;\n") is None


def test_jsts_adapter_registered_by_default() -> None:
    """AC #5: ``JSTSAdapter`` is registered at module import time —
    importing ``autofix_next.languages`` makes it resolvable by
    extension."""
    pytest.importorskip("autofix_next.languages.jsts")
    from autofix_next import languages

    adapter = languages.lookup_by_extension(".ts")
    assert adapter is not None
    assert adapter.language == "typescript"
