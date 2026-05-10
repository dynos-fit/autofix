"""Analyzer registry and standalone analyze_files entry point.

This module owns the canonical mapping from analyzer-set names to their
callable ``analyze`` functions, a correlation-context binding helper, and
the ``analyze_files`` function that drives the outer/inner analysis loop.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from autofix.analyzers.cheap.unused_import import analyze as _analyze_unused
from autofix.analyzers.linter_passthrough.eslint import analyze as _analyze_eslint
from autofix.analyzers.linter_passthrough.golangci import analyze as _analyze_golangci
from autofix.analyzers.linter_passthrough.ruff import analyze as _analyze_ruff
from autofix.analyzers.linter_passthrough.mypy import analyze as _analyze_mypy
from autofix.analyzers.llm_judgment.code_quality import CodeQualityJudgmentAnalyzer
from autofix.analyzers.llm_judgment.dead_code import DeadCodeJudgmentAnalyzer
from autofix.analyzers.llm_judgment.performance import PerformanceJudgmentAnalyzer
from autofix.analyzers.llm_judgment.security import SecurityJudgmentAnalyzer
from autofix.evidence.schema import CandidateFinding
from autofix.indexing.symbols import build_symbol_table
from autofix.parsing.tree_sitter import parse_file
from autofix.telemetry.correlation import _COMMIT_SHA, _SCAN_ID, _EVENT_ID

# Nine-key registry mapping analyzer-set names to their analyze callables.
# Keys are identical strings to those in autofix/funnel/pipeline.py;
# callables are the same objects (same import paths, no wrappers).
_ANALYZER_REGISTRY: dict[str, object] = {
    "cheap": _analyze_unused,
    "linter:eslint": _analyze_eslint,
    "linter:golangci": _analyze_golangci,
    "linter:ruff": _analyze_ruff,
    "linter:mypy": _analyze_mypy,
    "llm:code-quality": CodeQualityJudgmentAnalyzer.analyze,
    "llm:dead-code": DeadCodeJudgmentAnalyzer.analyze,
    "llm:performance": PerformanceJudgmentAnalyzer.analyze,
    "llm:security": SecurityJudgmentAnalyzer.analyze,
}


@contextmanager
def _bind_correlation_ctx(
    commit_sha: str | None,
    scan_id: str | None,
    event_id: str | None,
) -> Generator[None, None, None]:
    """Bind correlation ContextVars for the duration of a ``with`` block.

    Only non-``None`` args are set; ``None`` args are left at their
    current (or default) value. All tokens are reset in REVERSE set order
    on both normal exit and exception exit.
    """
    tokens = []
    try:
        if commit_sha is not None:
            tokens.append((_COMMIT_SHA, _COMMIT_SHA.set(commit_sha)))
        if scan_id is not None:
            tokens.append((_SCAN_ID, _SCAN_ID.set(scan_id)))
        if event_id is not None:
            tokens.append((_EVENT_ID, _EVENT_ID.set(event_id)))
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def _reset_passthrough_analyzer_state() -> None:
    """Clear per-scan memo dicts of every passthrough adapter.

    Audit SEC-RUFF-02 / cq-002 / SEC-RUFF-02-INCOMPLETE: must run on
    both success and exception paths so a long-running daemon does not
    leak one memo entry per scan_id when ``analyze_files`` raises.
    Cleanup must never raise — operators see a leak eventually rather
    than a hard failure now.
    """
    try:
        from autofix.analyzers.linter_passthrough import eslint as _eslint
        _eslint._reset_per_scan_state()
    except Exception:
        pass
    try:
        from autofix.analyzers.linter_passthrough import golangci as _golangci
        _golangci._reset_per_scan_state()
    except Exception:
        pass
    try:
        from autofix.analyzers.linter_passthrough import (
            ruff as _linter_ruff_mod,
        )
        _linter_ruff_mod._reset_per_scan_state()
    except Exception:
        pass
    try:
        from autofix.analyzers.linter_passthrough import (
            mypy as _linter_mypy_mod,
        )
        _linter_mypy_mod._reset_per_scan_state()
    except Exception:
        pass
    try:
        from autofix.analyzers.llm_judgment import _base as _llm_judgment_base
        _llm_judgment_base.LLMJudgmentAnalyzer._reset_per_scan_state()
    except Exception:
        pass


def analyze_files(
    files: list[Path],
    *,
    analyzers: list[str],
    repo_root: Path,
    commit_sha: str | None = None,
    scan_id: str | None = None,
    event_id: str | None = None,
) -> list[CandidateFinding]:
    """Run the requested analyzers over each file and return all findings.

    Parameters
    ----------
    files:
        Absolute or repo-relative paths to analyze.
    analyzers:
        List of analyzer-set names from :data:`_ANALYZER_REGISTRY`.
        Unknown names are skipped silently.
    repo_root:
        Repository root passed through to ``parse_file`` and
        ``build_symbol_table``.
    commit_sha:
        Optional SHA to bind into the correlation ContextVar for the
        duration of this call.
    scan_id:
        Optional scan identifier to bind into the correlation ContextVar.
    event_id:
        Optional event identifier to bind into the correlation ContextVar.

    Returns
    -------
    list[CandidateFinding]
        All findings produced across every (analyzer, file) pair, in
        outer-loop (analyzer) × inner-loop (file) traversal order.
    """
    findings: list[CandidateFinding] = []
    with _bind_correlation_ctx(commit_sha, scan_id, event_id):
        try:
            for analyzer_name in analyzers:
                callable_ = _ANALYZER_REGISTRY.get(analyzer_name)
                if callable_ is None:
                    continue
                for path in files:
                    try:
                        parse_result = parse_file(path, repo_root=repo_root)
                    except (OSError, FileNotFoundError, PermissionError):
                        continue
                    try:
                        symbol_table = build_symbol_table(parse_result)
                    except (NotImplementedError, OSError):
                        continue
                    try:
                        result = callable_(parse_result, symbol_table)
                        if hasattr(result, "__iter__") and not isinstance(result, list):
                            findings.extend(list(result))
                        else:
                            findings.extend(result)
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception as exc:
                        exc_type = type(exc).__name__
                        print(
                            f"autofix: warning: {analyzer_name} on {path} failed: "
                            f"{exc_type}: {exc!r}; continuing",
                            file=sys.stderr,
                        )
            return findings
        finally:
            _reset_passthrough_analyzer_state()
