"""Unit tests for ``autofix_next.invalidation.import_resolver``.

AC #9 pins these rules:

* ``from pkg.sub import name`` → ``<root>/pkg/sub/name.py`` OR
  ``<root>/pkg/sub.py`` with ``name`` as a symbol.
* ``import pkg.sub`` → ``<root>/pkg/sub.py`` or ``<root>/pkg/sub/__init__.py``.
* Aliases (``import x as y``, ``from x import y as z``) bind the alias.
* Relative imports, star imports, dynamic imports, third-party / stdlib
  (not under ``root``) return ``None`` and are listed as documented
  non-goals in the module docstring.

We write a small package tree to ``tmp_path`` and build ``ImportRecord``
instances manually so the resolver is exercised independently of the
tree-sitter walker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_record(bound_name: str, raw_text: str):
    """Construct an ``ImportRecord`` that the resolver will consume."""
    from autofix_next.indexing.symbols import ImportRecord

    return ImportRecord(
        bound_name=bound_name,
        raw_text=raw_text,
        start_line=1,
        end_line=1,
    )


@pytest.fixture()
def pkg_tree(tmp_path: Path) -> Path:
    """Build a tiny absolute-import fixture package at ``tmp_path``.

    Layout:

        pkg/__init__.py
        pkg/sub.py
        pkg/sub/__init__.py      (shadowed when file sub.py exists — we skip)
        pkg/other.py
        pkg/deep/__init__.py
        pkg/deep/name.py
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "sub.py").write_text(
        "def name():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "other.py").write_text("def thing():\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "deep").mkdir()
    (tmp_path / "pkg" / "deep" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "deep" / "name.py").write_text(
        "def go():\n    pass\n", encoding="utf-8"
    )
    return tmp_path


def _all_paths(root: Path) -> frozenset[str]:
    """Return repo-relative POSIX *.py paths under ``root``."""
    out: set[str] = set()
    for p in root.rglob("*.py"):
        rel = p.relative_to(root).as_posix()
        out.add(rel)
    return frozenset(out)


def test_resolver_absolute_from_import(pkg_tree: Path) -> None:
    """AC #9: ``from pkg.sub import name`` resolves to ``pkg/sub.py`` with
    ``name`` as the imported symbol."""
    from autofix_next.invalidation.import_resolver import resolve

    record = _make_record(bound_name="name", raw_text="from pkg.sub import name")
    resolved = resolve(
        record, repo_root=pkg_tree, all_paths=_all_paths(pkg_tree)
    )
    assert resolved is not None
    assert resolved.target_path == "pkg/sub.py"
    assert resolved.target_symbol == "name"


def test_resolver_import_module(pkg_tree: Path) -> None:
    """AC #9: ``import pkg.sub`` resolves to ``pkg/sub.py`` with no symbol."""
    from autofix_next.invalidation.import_resolver import resolve

    record = _make_record(bound_name="pkg", raw_text="import pkg.sub")
    resolved = resolve(
        record, repo_root=pkg_tree, all_paths=_all_paths(pkg_tree)
    )
    assert resolved is not None
    assert resolved.target_path == "pkg/sub.py"
    assert resolved.target_symbol is None


def test_resolver_aliased(pkg_tree: Path) -> None:
    """AC #9: ``from pkg.sub import name as n`` resolves and the alias ``n``
    is the bound name in the record."""
    from autofix_next.invalidation.import_resolver import resolve

    record = _make_record(bound_name="n", raw_text="from pkg.sub import name as n")
    resolved = resolve(
        record, repo_root=pkg_tree, all_paths=_all_paths(pkg_tree)
    )
    assert resolved is not None
    # The alias binds; the resolver still points to the real file + real symbol.
    assert resolved.target_path == "pkg/sub.py"
    assert resolved.target_symbol == "name"


def test_resolver_relative_returns_none(pkg_tree: Path) -> None:
    """AC #9: relative imports return ``None``."""
    from autofix_next.invalidation.import_resolver import resolve

    record = _make_record(bound_name="other", raw_text="from . import other")
    resolved = resolve(
        record, repo_root=pkg_tree, all_paths=_all_paths(pkg_tree)
    )
    assert resolved is None


def test_resolver_star_returns_none(pkg_tree: Path) -> None:
    """AC #9: ``from pkg.sub import *`` returns ``None`` (bound names unknown)."""
    from autofix_next.invalidation.import_resolver import resolve

    record = _make_record(bound_name="*", raw_text="from pkg.sub import *")
    resolved = resolve(
        record, repo_root=pkg_tree, all_paths=_all_paths(pkg_tree)
    )
    assert resolved is None


def test_resolver_third_party_returns_none(pkg_tree: Path) -> None:
    """AC #9: imports that do not resolve under ``repo_root`` return ``None``.

    Used for stdlib (``os``, ``re``) and third-party (``requests``, ``numpy``).
    """
    from autofix_next.invalidation.import_resolver import resolve

    for bound, raw in [
        ("os", "import os"),
        ("json", "import json"),
        ("path", "from os import path"),
        ("requests", "import requests"),
    ]:
        record = _make_record(bound_name=bound, raw_text=raw)
        resolved = resolve(
            record, repo_root=pkg_tree, all_paths=_all_paths(pkg_tree)
        )
        assert resolved is None, f"expected None for {raw!r}, got {resolved!r}"


def test_resolver_docstring_lists_non_goals() -> None:
    """AC #9: the module docstring explicitly enumerates each non-goal so
    operators can see which import classes will silently miss edges."""
    from autofix_next.invalidation import import_resolver

    doc = (import_resolver.__doc__ or "").lower()
    assert "relative" in doc, "docstring must list relative imports as a non-goal"
    assert "star" in doc or "wildcard" in doc, (
        "docstring must list star/wildcard imports as a non-goal"
    )
    assert "dynamic" in doc or "importlib" in doc, (
        "docstring must list dynamic imports as a non-goal"
    )
    assert "third-party" in doc or "third party" in doc or "stdlib" in doc, (
        "docstring must list third-party / stdlib imports as a non-goal"
    )
