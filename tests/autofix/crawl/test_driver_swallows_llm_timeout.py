"""Regression: a single LLM-CLI timeout must not abort the crawl cycle.

Surfaced when running ``autofix --root . --once`` with the
aggressive budget — the ``claude`` CLI hit the scheduler's
60-second timeout on one bundle, which propagated as
``subprocess.TimeoutExpired`` through ``_analyze_bundle`` and
killed the cycle.

Two-layer fix:

1. :mod:`autofix.analyzers.llm_judgment._base` — the analyzer's
   own ``analyze`` classmethod now catches
   ``subprocess.TimeoutExpired`` and ``OSError`` from the
   scheduler invoke, logs an ``AnalyzerTimeout`` event once per
   scan, and returns an empty iter.
2. :mod:`autofix.crawl.driver._analyze_bundle` — last-resort
   safety net catches any ``Exception`` from the analyzer
   callable, logs to stderr, and continues to the next file.
   ``KeyboardInterrupt`` / ``SystemExit`` still propagate.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_bundle(tmp_path: Path) -> object:
    from autofix.crawl.bundles import Bundle

    src = tmp_path / "x.py"
    src.write_text("import os\n")
    file_paths = (src,)
    return Bundle(
        seed_path=src,
        file_paths=file_paths,
        total_bytes=src.stat().st_size,
        fingerprint=Bundle.compute_fingerprint(file_paths),
    )


def test_analyze_bundle_swallows_timeout_expired(tmp_path: Path) -> None:
    """A registry callable that raises TimeoutExpired must not propagate."""
    from autofix.crawl.driver import _analyze_bundle

    bundle = _make_bundle(tmp_path)

    def _raises(parse_result, symbol_table):
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=60)

    with patch.dict(
        "autofix.funnel.pipeline._ANALYZER_REGISTRY",
        {"llm:security": _raises}, clear=True,
    ):
        findings = _analyze_bundle(
            bundle=bundle, analyzer="llm:security",
            root=tmp_path, commit_sha="abc",
        )

    # Cycle survived; no findings produced for this file.
    assert findings == []


def test_analyze_bundle_swallows_runtime_error(tmp_path: Path) -> None:
    """Any other unexpected analyzer exception is also swallowed."""
    from autofix.crawl.driver import _analyze_bundle

    bundle = _make_bundle(tmp_path)

    def _raises(parse_result, symbol_table):
        raise RuntimeError("simulated transient failure")

    with patch.dict(
        "autofix.funnel.pipeline._ANALYZER_REGISTRY",
        {"cheap": _raises}, clear=True,
    ):
        findings = _analyze_bundle(
            bundle=bundle, analyzer="cheap",
            root=tmp_path, commit_sha="abc",
        )

    assert findings == []


def test_analyze_bundle_keyboard_interrupt_propagates(tmp_path: Path) -> None:
    """KeyboardInterrupt must escape the safety-net so Ctrl-C still works."""
    from autofix.crawl.driver import _analyze_bundle

    bundle = _make_bundle(tmp_path)

    def _kbd(parse_result, symbol_table):
        raise KeyboardInterrupt

    with patch.dict(
        "autofix.funnel.pipeline._ANALYZER_REGISTRY",
        {"cheap": _kbd}, clear=True,
    ):
        with pytest.raises(KeyboardInterrupt):
            _analyze_bundle(
                bundle=bundle, analyzer="cheap",
                root=tmp_path, commit_sha="abc",
            )


def test_analyze_bundle_system_exit_propagates(tmp_path: Path) -> None:
    """SystemExit also escapes the safety-net (``sys.exit(N)`` must work)."""
    from autofix.crawl.driver import _analyze_bundle

    bundle = _make_bundle(tmp_path)

    def _exit(parse_result, symbol_table):
        raise SystemExit(2)

    with patch.dict(
        "autofix.funnel.pipeline._ANALYZER_REGISTRY",
        {"cheap": _exit}, clear=True,
    ):
        with pytest.raises(SystemExit):
            _analyze_bundle(
                bundle=bundle, analyzer="cheap",
                root=tmp_path, commit_sha="abc",
            )


def test_llm_judgment_base_swallows_subprocess_timeout(tmp_path: Path) -> None:
    """The analyzer base class itself catches ``TimeoutExpired`` from
    ``Scheduler.invoke_judgment`` and returns no findings."""
    from autofix.analyzers.llm_judgment._base import LLMJudgmentAnalyzer

    class _Stub(LLMJudgmentAnalyzer):
        RULE_ID_PREFIX = "llm:test"
        MODEL = "test-model"

        @classmethod
        def prompt_template(cls, diff_context: str) -> str:
            return f"test:\n{diff_context}"

    src = tmp_path / "x.py"
    src.write_text("# trivial\n")
    parse_result = MagicMock()
    parse_result.path = src
    parse_result.relpath = "x.py"
    parse_result.commit_sha = "abc"

    def _scheduler_raises(*_a, **_k):
        sched = MagicMock()
        sched.invoke_judgment.side_effect = subprocess.TimeoutExpired(
            cmd=["claude"], timeout=60,
        )
        return sched

    with patch(
        "autofix.analyzers.llm_judgment._base.Scheduler",
        side_effect=_scheduler_raises,
    ):
        result = list(_Stub.analyze(parse_result, symbol_table=None))

    assert result == []
