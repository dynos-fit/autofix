"""TDD tests for the widened analyze_files signature.

Per design-decisions.md D1 + spec.md AC-9: analyze_files accepts
list[str | Callable]. String items look up in registry; callable
items are used directly. The unknown-name warning fires only for
string items not in the registry -- callables are exempt.
"""
from __future__ import annotations

import io
from contextlib import redirect_stderr
from pathlib import Path
from typing import Mapping

import pytest


def test_analyzer_registry_is_public_mapping():
    """AC-5: ANALYZER_REGISTRY is exposed at autofix.analyzers as
    a read-only Mapping. Mutating it raises TypeError.
    """
    from autofix.analyzers import ANALYZER_REGISTRY
    assert isinstance(ANALYZER_REGISTRY, Mapping)
    # MappingProxyType raises TypeError on mutation.
    with pytest.raises(TypeError):
        ANALYZER_REGISTRY["new_key"] = lambda *a, **kw: []


def test_analyzer_registry_has_expected_keys():
    """AC-5: the public registry contains the same 9 keys as before."""
    from autofix.analyzers import ANALYZER_REGISTRY
    expected = {
        "cheap",
        "linter:eslint",
        "linter:golangci",
        "linter:ruff",
        "linter:mypy",
        "llm:code-quality",
        "llm:dead-code",
        "llm:performance",
        "llm:security",
    }
    assert set(ANALYZER_REGISTRY.keys()) == expected


def test_reset_passthrough_state_is_public():
    """AC-6: reset_passthrough_state is a public alias of the
    private _reset_passthrough_analyzer_state."""
    from autofix.analyzers import reset_passthrough_state
    from autofix.analyzers._registry import _reset_passthrough_analyzer_state
    # They must be the SAME callable object (alias, not a wrapper).
    assert reset_passthrough_state is _reset_passthrough_analyzer_state


def test_analyze_files_accepts_callable_in_list():
    """AC-9: analyze_files accepts a Callable directly in the analyzers list."""
    from autofix.analyzers import ANALYZER_REGISTRY, analyze_files
    cheap_callable = ANALYZER_REGISTRY["cheap"]
    # Must not raise; empty file list returns empty findings.
    result = analyze_files(
        [], analyzers=[cheap_callable], repo_root=Path(".")
    )
    assert result == []


def test_analyze_files_accepts_mixed_string_and_callable():
    """AC-9: analyze_files accepts a mixed list of name strings and callables."""
    from autofix.analyzers import ANALYZER_REGISTRY, analyze_files
    cheap_callable = ANALYZER_REGISTRY["cheap"]
    result = analyze_files(
        [], analyzers=["cheap", cheap_callable], repo_root=Path(".")
    )
    assert result == []


def test_analyze_files_string_only_byte_identical_with_pre_widening():
    """AC-10: name-only callers see byte-identical behavior.
    A list[str] call must not change behavior or output.
    """
    from autofix.analyzers import analyze_files
    # No files, valid name -> empty findings, no exception, no warning.
    buf = io.StringIO()
    with redirect_stderr(buf):
        result = analyze_files(
            [], analyzers=["cheap"], repo_root=Path(".")
        )
    assert result == []
    assert "warning" not in buf.getvalue()


def test_analyze_files_unknown_string_warns():
    """AC-7 + AC-8: unknown string items still produce the P-100
    stderr warning."""
    from autofix.analyzers import analyze_files
    buf = io.StringIO()
    with redirect_stderr(buf):
        analyze_files(
            [], analyzers=["unknown_typo"], repo_root=Path(".")
        )
    assert "unknown analyzer 'unknown_typo'" in buf.getvalue()


def test_analyze_files_callable_does_not_warn():
    """AC-7 + AC-8: callable items are exempt from the unknown-name warning.
    Only string items participate in the registry-membership check.
    """
    from autofix.analyzers import ANALYZER_REGISTRY, analyze_files
    cheap_callable = ANALYZER_REGISTRY["cheap"]
    buf = io.StringIO()
    with redirect_stderr(buf):
        analyze_files(
            [], analyzers=[cheap_callable], repo_root=Path(".")
        )
    assert "warning" not in buf.getvalue()


def test_analyze_files_callable_does_not_raise_on_unhashable():
    """AC-9 / D7: the validation pass MUST guard with isinstance(item, str)
    before testing membership. Otherwise unhashable Callable objects
    would crash the validator with TypeError.
    """
    from autofix.analyzers import analyze_files

    # A "callable" object that is intentionally not hashable (the
    # registry's ``__contains__`` would call __hash__ on it). The
    # validator must skip it via isinstance(item, str) guard.
    class _UnhashableCallable:
        __hash__ = None  # type: ignore[assignment]
        def __call__(self, parse_result, symbol_table):
            return []

    item = _UnhashableCallable()
    # Must not raise TypeError.
    result = analyze_files(
        [], analyzers=[item], repo_root=Path(".")
    )
    assert result == []
