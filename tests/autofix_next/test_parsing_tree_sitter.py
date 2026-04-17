"""Tests for autofix_next.parsing.tree_sitter.

Covers AC #21 tree-sitter minimum requirements:
  - test_parse_file_returns_parse_result
  - test_grammar_abi_mismatch_surfaces_tree_sitter_load_error
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_parse_file_returns_parse_result(tmp_path: Path) -> None:
    """parse_file on a trivial Python file returns a ParseResult with a tree,
    the raw source bytes, and a repo-relative (or absolute) path."""
    from autofix_next.parsing.tree_sitter import parse_file

    src = "import os\n\nx = os.getcwd()\n"
    target = tmp_path / "sample.py"
    target.write_text(src, encoding="utf-8")

    result = parse_file(target)

    assert result is not None
    # ParseResult must expose the tree, source bytes, and a file path.
    assert hasattr(result, "tree")
    assert result.tree is not None
    assert hasattr(result, "source_bytes")
    assert isinstance(result.source_bytes, (bytes, bytearray))
    assert bytes(result.source_bytes) == src.encode("utf-8")
    # A path attribute must exist (name varies: `path`, `relpath`, etc.).
    assert any(
        hasattr(result, attr) for attr in ("path", "relpath", "file_path")
    ), f"ParseResult missing a path attribute: {dir(result)!r}"


def test_grammar_abi_mismatch_surfaces_tree_sitter_load_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the tree-sitter grammar fails to load (e.g. ABI mismatch), the
    parsing layer surfaces a TreeSitterLoadError with a clear message."""
    from autofix_next.parsing import tree_sitter as ts_mod

    assert hasattr(ts_mod, "TreeSitterLoadError"), (
        "autofix_next.parsing.tree_sitter must expose TreeSitterLoadError"
    )

    def _fake_loader(*args, **kwargs):  # pragma: no cover - injected failure
        raise RuntimeError("Incompatible Language version 14. Must be between 13 and 13")

    # The parser must either call a named loader we can patch, or catch
    # the load error from tree_sitter and re-raise TreeSitterLoadError.
    # Strategy: patch a private `_load_language` or equivalent helper.
    target = tmp_path / "sample.py"
    target.write_text("import os\n", encoding="utf-8")

    # The parser must expose an introspectable language loader we can patch
    # to simulate ABI-mismatch errors. Contract: one of _load_language /
    # _get_language / _language exists on the module.
    loader_attr = None
    for candidate in ("_load_language", "_get_language", "_language", "_LANGUAGE"):
        if hasattr(ts_mod, candidate):
            loader_attr = candidate
            break
    assert loader_attr is not None, (
        "autofix_next.parsing.tree_sitter must expose a patchable language "
        "loader (_load_language / _get_language / _language) so ABI-mismatch "
        "handling can be exercised."
    )

    monkeypatch.setattr(ts_mod, loader_attr, _fake_loader, raising=True)

    with pytest.raises(ts_mod.TreeSitterLoadError):
        ts_mod.parse_file(target)
