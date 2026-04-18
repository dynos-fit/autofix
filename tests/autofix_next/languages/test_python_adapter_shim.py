"""Tests for the Python-parser shim (task-006 AC #9, #10, #11, #41).

After task-006 seg-2, ``autofix_next/parsing/tree_sitter.py`` is reduced
to a thin re-export shim pointing at ``autofix_next/languages/python.py``.
The shim must forward both READS (import / getattr) and WRITES
(monkeypatching) to the underlying module so every task-001..005 test
continues to work.

This file pins:

* AC #41: ``parse_file`` identity across the two paths.
* AC #41: ``ParseResult`` class identity across the two paths.
* AC #9 / risk-note #2: ``monkeypatch.setattr(ts_mod, "_load_language", ...)``
  on the shim triggers the underlying module's ``_ensure_parser`` cache
  rebuild (i.e. the patched loader is actually called).

Collection-time tolerance: these tests use ``pytest.importorskip`` for
``autofix_next.languages.python`` so the file can be collected before
seg-2 lands. Once seg-2 lands, the skips turn into real assertions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# AC #41 — identity across shim and underlying module
# ---------------------------------------------------------------------------


def test_parse_file_identity() -> None:
    """AC #41: ``autofix_next.parsing.tree_sitter.parse_file`` is the same
    function object as ``autofix_next.languages.python.parse_file``.
    """
    pytest.importorskip("autofix_next.languages.python")
    from autofix_next.languages import python as lang_python
    from autofix_next.parsing import tree_sitter as ts_mod

    assert ts_mod.parse_file is lang_python.parse_file, (
        "shim must re-export parse_file as the same function object"
    )


def test_parseresult_identity() -> None:
    """AC #41: ``ParseResult`` class is the same object on both paths.

    A separate class would defeat ``isinstance`` checks at the call sites.
    """
    pytest.importorskip("autofix_next.languages.python")
    from autofix_next.languages import python as lang_python
    from autofix_next.parsing import tree_sitter as ts_mod

    assert ts_mod.ParseResult is lang_python.ParseResult, (
        "shim must re-export ParseResult as the same class object"
    )


def test_tree_sitter_load_error_identity() -> None:
    """AC #9: ``TreeSitterLoadError`` class is the same object on both
    paths. Catching it via one path must catch raises from the other.
    """
    pytest.importorskip("autofix_next.languages.python")
    from autofix_next.languages import python as lang_python
    from autofix_next.parsing import tree_sitter as ts_mod

    assert ts_mod.TreeSitterLoadError is lang_python.TreeSitterLoadError, (
        "shim must re-export TreeSitterLoadError as the same class"
    )


def test_load_language_identity() -> None:
    """AC #9: the patchable ``_load_language`` attribute is the same
    callable object on both paths at shim-import time (pre-monkeypatch).
    """
    pytest.importorskip("autofix_next.languages.python")
    from autofix_next.languages import python as lang_python
    from autofix_next.parsing import tree_sitter as ts_mod

    assert ts_mod._load_language is lang_python._load_language, (
        "shim must re-export _load_language as the same function object"
    )


# ---------------------------------------------------------------------------
# AC #9 / risk-note #2 — monkeypatch-through-shim semantics
# ---------------------------------------------------------------------------


def test_shim_monkeypatch_visible_to_underlying_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Risk-note #2 mitigation: ``monkeypatch.setattr(ts_mod, "_load_language",
    counting_fn)`` on the shim MUST cause the underlying
    ``_ensure_parser`` cache check to invoke ``counting_fn``.

    This is the contract that existing tests
    (``test_parsing_tree_sitter.py::test_grammar_abi_mismatch_surfaces_tree_sitter_load_error``,
    plus ``test_call_graph_wrapper.py`` / ``test_scip_index.py``) rely on.
    A naive static ``from ... import _load_language`` shim would break
    this because reassigning the shim attribute would not rebind the
    name looked up via ``globals().get("_load_language")`` inside
    ``_ensure_parser`` in the underlying module.
    """
    pytest.importorskip("autofix_next.languages.python")
    pytest.importorskip("tree_sitter_python")
    pytest.importorskip("tree_sitter")
    from autofix_next.parsing import tree_sitter as ts_mod

    # Prime the parser cache with the real loader, so the "loader swapped"
    # identity check triggers a rebuild when we patch.
    target = tmp_path / "prime.py"
    target.write_text("import os\n", encoding="utf-8")
    ts_mod.parse_file(target)

    # Counting loader: records how many times it is invoked.
    calls = {"n": 0}
    real_loader = ts_mod._load_language

    def counting_loader(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real_loader(*args, **kwargs)

    monkeypatch.setattr(ts_mod, "_load_language", counting_loader)

    # The monkeypatch swap must invalidate the parser cache in the
    # underlying module and cause counting_loader to be invoked on the
    # next parse.
    target2 = tmp_path / "post_patch.py"
    target2.write_text("import os\n", encoding="utf-8")
    ts_mod.parse_file(target2)

    assert calls["n"] >= 1, (
        "monkeypatch.setattr(ts_mod, '_load_language', ...) on the shim "
        "must cause the underlying module's _ensure_parser cache check "
        "to see the new loader and invoke it. Got zero calls — the "
        "shim forwarding is broken."
    )


def test_shim_preserves_all_attribute() -> None:
    """AC #9: the shim's ``__all__`` exposes at least the three public
    names (``TreeSitterLoadError``, ``ParseResult``, ``parse_file``)
    that the original module exported."""
    from autofix_next.parsing import tree_sitter as ts_mod

    assert hasattr(ts_mod, "__all__"), (
        "shim must declare __all__ (mirrors original module's contract)"
    )
    all_names = set(ts_mod.__all__)
    required = {"TreeSitterLoadError", "ParseResult", "parse_file"}
    assert required.issubset(all_names), (
        f"shim __all__ must contain {required!r}, got {all_names!r}"
    )


def test_python_adapter_registered() -> None:
    """AC #8: importing ``autofix_next.languages.python`` registers a
    ``PythonAdapter`` (language='python', extensions=('.py',))."""
    pytest.importorskip("autofix_next.languages.python")
    from autofix_next import languages

    py_adapters = [a for a in languages.all_adapters() if a.language == "python"]
    assert len(py_adapters) == 1, (
        f"exactly one python adapter must be registered, got {len(py_adapters)}"
    )
    adapter = py_adapters[0]
    assert type(adapter).__name__ == "PythonAdapter"
    assert adapter.extensions == (".py",)


def test_python_adapter_parse_precise_returns_none() -> None:
    """AC #7: ``PythonAdapter.parse_precise(source)`` returns ``None``
    (precision path is not implemented for Python in this task)."""
    pytest.importorskip("autofix_next.languages.python")
    from autofix_next.languages.python import PythonAdapter

    adapter = PythonAdapter()
    assert adapter.parse_precise(b"x = 1\n") is None


def test_python_adapter_language_and_extensions() -> None:
    """AC #7: ``PythonAdapter.language == 'python'``, extensions = ('.py',)."""
    pytest.importorskip("autofix_next.languages.python")
    from autofix_next.languages.python import PythonAdapter

    adapter = PythonAdapter()
    assert adapter.language == "python"
    assert adapter.extensions == (".py",)
