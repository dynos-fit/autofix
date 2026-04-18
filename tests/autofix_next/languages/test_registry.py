"""Tests for ``autofix_next.languages`` registry (task-006 AC #1..#5,
#38).

Covers
------
* AC #1 — public names exported via ``__all__``.
* AC #2 — ``LanguageAdapter`` is ``@runtime_checkable`` (covered by
  ``test_language_adapter_is_runtime_checkable``).
* AC #3 — ``lookup_by_extension`` / ``lookup_by_language`` / etc.
* AC #4 — idempotent-silent duplicate registration.
* AC #5 — registration order is Python, JS/TS, Go.
* AC #38 — the exact extension mapping table.
"""

from __future__ import annotations

from typing import Any

import pytest


def _import_languages():
    """Import the registry module; skip gracefully if not yet landed."""
    return pytest.importorskip("autofix_next.languages")


# ---------------------------------------------------------------------------
# AC #1 / #3 — public surface
# ---------------------------------------------------------------------------


def test_languages_module_exports_expected_public_names() -> None:
    """AC #1: the package re-exports the six public names via ``__all__``."""
    languages = _import_languages()
    expected = {
        "LanguageAdapter",
        "register",
        "lookup_by_extension",
        "lookup_by_language",
        "all_adapters",
        "all_extensions",
    }
    assert hasattr(languages, "__all__"), (
        "autofix_next.languages must declare __all__"
    )
    assert expected.issubset(set(languages.__all__)), (
        f"autofix_next.languages.__all__ must be a superset of {expected!r}"
    )
    for name in expected:
        assert hasattr(languages, name), (
            f"autofix_next.languages must expose {name!r}"
        )


# ---------------------------------------------------------------------------
# AC #2 — runtime_checkable Protocol
# ---------------------------------------------------------------------------


def test_language_adapter_is_runtime_checkable() -> None:
    """AC #2: ``isinstance(adapter, LanguageAdapter)`` works because the
    Protocol is declared ``@runtime_checkable``. We confirm the class
    carries the ``_is_runtime_protocol`` sentinel typing sets and that
    ``isinstance`` actually succeeds for a concrete registered adapter.
    """
    languages = _import_languages()
    LanguageAdapter = languages.LanguageAdapter

    # The typing module tags runtime-checkable protocols with this attribute.
    assert getattr(LanguageAdapter, "_is_runtime_protocol", False), (
        "LanguageAdapter must be decorated with @runtime_checkable"
    )

    adapters = languages.all_adapters()
    assert len(adapters) >= 1, (
        "at least one adapter (PythonAdapter) must be registered"
    )
    # runtime isinstance must succeed for every registered adapter.
    for adapter in adapters:
        assert isinstance(adapter, LanguageAdapter), (
            f"registered adapter {adapter!r} must satisfy "
            "isinstance(adapter, LanguageAdapter)"
        )


# ---------------------------------------------------------------------------
# AC #3 / #38 — extension -> adapter mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ext,expected_attr",
    [
        (".py", "PythonAdapter"),
        (".ts", "JSTSAdapter"),
        (".tsx", "JSTSAdapter"),
        (".js", "JSTSAdapter"),
        (".jsx", "JSTSAdapter"),
        (".go", "GoAdapter"),
    ],
)
def test_lookup_by_extension_returns_correct_adapter(
    ext: str, expected_attr: str
) -> None:
    """AC #3 / #38: each registered extension resolves to the expected
    adapter class by name. We compare class names rather than importing
    the classes directly so the test is robust against module-layout
    shuffles during implementation.
    """
    languages = _import_languages()
    adapter = languages.lookup_by_extension(ext)
    assert adapter is not None, (
        f"lookup_by_extension({ext!r}) must not return None"
    )
    assert type(adapter).__name__ == expected_attr, (
        f"lookup_by_extension({ext!r}) must return a {expected_attr}, "
        f"got {type(adapter).__name__!r}"
    )


@pytest.mark.parametrize(
    "ext",
    [".mjs", ".cjs", ".d.ts", ".vue", ".svelte", ".rs"],
)
def test_lookup_by_extension_unknown_returns_none(ext: str) -> None:
    """AC #3 / #38: unrecognized extensions return None (no warning, no
    error, no default)."""
    languages = _import_languages()
    result = languages.lookup_by_extension(ext)
    assert result is None, (
        f"lookup_by_extension({ext!r}) must return None, got {result!r}"
    )


def test_all_extensions_exact_set() -> None:
    """AC #38: ``all_extensions()`` contains exactly the six registered
    extensions — no more, no fewer."""
    languages = _import_languages()
    expected = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".go"})
    actual = frozenset(languages.all_extensions())
    assert actual == expected, (
        f"all_extensions() must equal {expected!r}, got {actual!r}"
    )


# ---------------------------------------------------------------------------
# AC #4 — idempotent-silent duplicate registration
# ---------------------------------------------------------------------------


def test_register_idempotent_silent() -> None:
    """AC #4: calling ``register(adapter)`` a second time for an adapter
    whose ``language`` is already registered is a silent no-op — no
    exception, no duplicate entry in ``all_adapters()``."""
    languages = _import_languages()

    before = languages.all_adapters()
    before_count = len(before)
    before_py = [a for a in before if a.language == "python"]
    assert before_py, "PythonAdapter must be registered before this test"

    # Construct a fresh PythonAdapter-like duplicate and re-register it.
    # The registry must first-win on language: the existing adapter
    # instance stays, and no new row is appended.
    duplicate = before_py[0]  # same language; same adapter instance is fine
    # Must not raise.
    languages.register(duplicate)

    after = languages.all_adapters()
    assert len(after) == before_count, (
        f"register() of duplicate language must be a no-op; "
        f"count went {before_count} -> {len(after)}"
    )
    # Exactly one adapter with language "python".
    py_count = sum(1 for a in after if a.language == "python")
    assert py_count == 1, (
        f"exactly one python adapter must remain registered, got {py_count}"
    )


# ---------------------------------------------------------------------------
# AC #3 — lookup_by_language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "language_name,expected_attr",
    [
        ("python", "PythonAdapter"),
        ("typescript", "JSTSAdapter"),
        ("go", "GoAdapter"),
    ],
)
def test_lookup_by_language_matches_name(
    language_name: str, expected_attr: str
) -> None:
    """AC #3: ``lookup_by_language`` matches on the ``.language`` attr."""
    languages = _import_languages()
    adapter = languages.lookup_by_language(language_name)
    assert adapter is not None, (
        f"lookup_by_language({language_name!r}) must not return None"
    )
    assert type(adapter).__name__ == expected_attr, (
        f"lookup_by_language({language_name!r}) must return {expected_attr}, "
        f"got {type(adapter).__name__!r}"
    )


def test_lookup_by_language_unknown_returns_none() -> None:
    """AC #3: unknown language name returns ``None``."""
    languages = _import_languages()
    assert languages.lookup_by_language("rust") is None
    assert languages.lookup_by_language("") is None


# ---------------------------------------------------------------------------
# AC #5 — registration order
# ---------------------------------------------------------------------------


def test_all_adapters_registration_order() -> None:
    """AC #5: adapters register in the exact order Python, JS/TS, Go.

    The side-effect imports at the end of ``autofix_next/languages/__init__.py``
    are ``python, jsts, go`` in that exact order, and each submodule
    self-registers via ``register(...)`` at its own module-end.
    """
    languages = _import_languages()
    adapters = languages.all_adapters()
    assert isinstance(adapters, tuple), (
        "all_adapters() must return a tuple for immutability"
    )
    # Filter to only the three canonical languages. We skip any future
    # adapter that later tasks may register so this test doesn't break
    # as a side effect of unrelated work.
    canonical_langs = {"python", "typescript", "go"}
    canonical = [a.language for a in adapters if a.language in canonical_langs]
    assert canonical == ["python", "typescript", "go"], (
        "registration order must be python, typescript, go; "
        f"got {canonical!r}"
    )


# ---------------------------------------------------------------------------
# AC #3 — all_extensions return type
# ---------------------------------------------------------------------------


def test_all_extensions_return_type_is_frozenset() -> None:
    """AC #3: ``all_extensions()`` returns a ``frozenset[str]`` (immutable)."""
    languages = _import_languages()
    exts = languages.all_extensions()
    assert isinstance(exts, frozenset), (
        f"all_extensions() must return frozenset, got {type(exts).__name__}"
    )


def test_all_adapters_return_type_is_tuple() -> None:
    """AC #3: ``all_adapters()`` returns an immutable tuple."""
    languages = _import_languages()
    adapters = languages.all_adapters()
    assert isinstance(adapters, tuple), (
        f"all_adapters() must return tuple, got {type(adapters).__name__}"
    )


# ---------------------------------------------------------------------------
# Case-sensitivity + dot-form discipline
# ---------------------------------------------------------------------------


def test_lookup_by_extension_is_case_sensitive() -> None:
    """AC #3: ``lookup_by_extension`` is case-sensitive. ``".PY"`` must
    NOT match the Python adapter (callers are expected to pass the
    canonical ``Path.suffix`` form)."""
    languages = _import_languages()
    assert languages.lookup_by_extension(".PY") is None


def test_lookup_by_extension_requires_leading_dot() -> None:
    """AC #3: ``"py"`` (missing leading dot) does NOT match."""
    languages = _import_languages()
    assert languages.lookup_by_extension("py") is None
