"""Tests for the analyzer-set resolution path on `autofix run`.

Two related behaviors:

* When ``--auto-llm`` is set without an explicit ``--analyzers``,
  expand to the documented LLM bug-finder set
  (:data:`DEFAULT_AUTO_LLM_ANALYZERS`). Closes the bad-default
  footgun where ``--auto-llm`` only enabled the LLM patcher tier
  but the analyzer set still defaulted to ``cheap`` only.
* When ``--auto-llm`` is set WITH an explicit ``--analyzers`` that
  contains no ``llm:*`` entry, emit a stderr warning so the
  operator notices that LLM patches won't fire on most findings.
"""
from __future__ import annotations

import argparse
import io
import sys

import pytest

from autofix.cli.run_command import _resolve_analyzer_set, add_arguments
from autofix.cli.run_constants import DEFAULT_AUTO_LLM_ANALYZERS


def _parse(*flags: str) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    add_arguments(p)
    return p.parse_args(["--root", "/tmp", *flags])


def test_bare_run_returns_none() -> None:
    """No --auto-llm, no --analyzers → funnel default applies."""
    ns = _parse()
    assert _resolve_analyzer_set(ns) is None


def test_auto_llm_without_explicit_set_expands_to_llm_default() -> None:
    """The fix for the operator footgun."""
    ns = _parse("--apply", "--auto-llm")
    resolved = _resolve_analyzer_set(ns)
    assert resolved == list(DEFAULT_AUTO_LLM_ANALYZERS)
    # The expansion must include cheap + every LLM bug-finder, in
    # canonical order.
    assert resolved == [
        "cheap",
        "llm:security",
        "llm:code-quality",
        "llm:dead-code",
        "llm:performance",
    ]


def test_explicit_analyzers_without_auto_llm_preserved() -> None:
    """Explicit --analyzers wins; no auto-llm means no warning either."""
    ns = _parse("--analyzers", "cheap,linter:ruff")
    assert _resolve_analyzer_set(ns) == ["cheap", "linter:ruff"]


def test_explicit_analyzers_with_auto_llm_and_llm_member_preserved_no_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explicit set wins; if it has any llm:*, no warning."""
    ns = _parse("--apply", "--auto-llm", "--analyzers", "cheap,llm:security")
    resolved = _resolve_analyzer_set(ns)
    assert resolved == ["cheap", "llm:security"]
    err = capsys.readouterr().err
    assert err == ""


def test_explicit_analyzers_with_auto_llm_no_llm_member_emits_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The other half of the footgun — operator chose --auto-llm but
    forgot to put any llm:* in --analyzers. Warn loudly."""
    ns = _parse("--apply", "--auto-llm", "--analyzers", "cheap,linter:ruff")
    resolved = _resolve_analyzer_set(ns)
    assert resolved == ["cheap", "linter:ruff"]
    err = capsys.readouterr().err
    assert "warning" in err.lower()
    assert "--auto-llm" in err
    assert "no llm:* analyzer" in err


def test_warning_suppressed_under_quiet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--quiet silences the warning (parity with the rest of the CLI's
    quiet discipline)."""
    ns = _parse(
        "--apply", "--auto-llm",
        "--analyzers", "cheap,linter:ruff",
        "--quiet",
    )
    _resolve_analyzer_set(ns)
    err = capsys.readouterr().err
    assert err == ""


def test_default_auto_llm_analyzers_constant_is_canonical() -> None:
    """Constants module pins the canonical 5-tuple. Adding/removing
    LLM bug-finders is a deliberate API change that bumps this list."""
    assert DEFAULT_AUTO_LLM_ANALYZERS == (
        "cheap",
        "llm:security",
        "llm:code-quality",
        "llm:dead-code",
        "llm:performance",
    )


def test_auto_llm_plus_empty_analyzers_string_still_expands() -> None:
    """``--analyzers ''`` is the same as not specifying --analyzers
    (treated as empty after .strip())."""
    ns = _parse("--apply", "--auto-llm", "--analyzers", "")
    assert _resolve_analyzer_set(ns) == list(DEFAULT_AUTO_LLM_ANALYZERS)


def test_auto_llm_plus_whitespace_analyzers_still_expands() -> None:
    """Pure whitespace also collapses to the empty case."""
    ns = _parse("--apply", "--auto-llm", "--analyzers", "   ,  ,  ")
    assert _resolve_analyzer_set(ns) == list(DEFAULT_AUTO_LLM_ANALYZERS)
