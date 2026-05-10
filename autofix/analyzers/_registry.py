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


# Per-scan cleanup target list. Each entry is
# ``(import_path, attr_path)`` resolved lazily inside
# :func:`_reset_passthrough_analyzer_state`.
#
# Adding a new analyzer with per-scan state means appending one tuple
# here, NOT copy-pasting another try/except block. The lazy-import
# shape preserves the previous behavior (a missing module is silently
# skipped because the import itself raises).
_PASSTHROUGH_RESET_TARGETS: tuple[tuple[str, str], ...] = (
    ("autofix.analyzers.linter_passthrough.eslint", "_reset_per_scan_state"),
    ("autofix.analyzers.linter_passthrough.golangci", "_reset_per_scan_state"),
    ("autofix.analyzers.linter_passthrough.ruff", "_reset_per_scan_state"),
    ("autofix.analyzers.linter_passthrough.mypy", "_reset_per_scan_state"),
    (
        "autofix.analyzers.llm_judgment._base",
        "LLMJudgmentAnalyzer._reset_per_scan_state",
    ),
)


def _emit_unknown_analyzer_warning(unknown_names: list[str]) -> None:
    """Print a stderr warning naming unknown analyzers + closest registry keys.

    PROACTIVE-05: silent registry-miss → zero findings is a real
    foot-gun. ``--analyzers ruf`` (typo of ``linter:ruff``) used to
    return a clean green scan with no signal. Now it also lands a
    warning line on stderr that points at the closest known name(s).

    Cleanup must never raise — telemetry/UX loss never aborts a scan.
    """
    import difflib
    import sys
    try:
        known = sorted(_ANALYZER_REGISTRY.keys())
        for name in unknown_names:
            close = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
            suggestion = (
                f" Did you mean: {', '.join(close)}?"
                if close
                else f" Known names: {', '.join(known)}."
            )
            print(
                f"autofix: warning: unknown analyzer {name!r}; skipped.{suggestion}",
                file=sys.stderr,
                flush=True,
            )
    except Exception:
        pass


def _reset_passthrough_analyzer_state() -> None:
    """Clear per-scan memo dicts of every passthrough adapter.

    Audit SEC-RUFF-02 / cq-002 / SEC-RUFF-02-INCOMPLETE: must run on
    both success and exception paths so a long-running daemon does not
    leak one memo entry per scan_id when ``analyze_files`` raises.
    Cleanup must never raise — operators see a leak eventually rather
    than a hard failure now.

    Iterates :data:`_PASSTHROUGH_RESET_TARGETS`. Each target is
    resolved by import + attribute walk (so ``ClassName.method`` works
    for the LLM-judgment class-level reset). Any exception in the
    resolve-or-call path is swallowed: the failure mode of cleanup is
    "no-op", not "explode the cycle".
    """
    import importlib
    for module_path, attr_path in _PASSTHROUGH_RESET_TARGETS:
        try:
            obj: object = importlib.import_module(module_path)
            for attr in attr_path.split("."):
                obj = getattr(obj, attr)
            obj()  # type: ignore[operator]
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
            unknown_names = [
                n for n in analyzers if n not in _ANALYZER_REGISTRY
            ]
            if unknown_names:
                # PROACTIVE-05: a typo'd analyzer name used to silently
                # produce zero findings — the loop just skipped unknown
                # entries with no signal beyond a JSONL telemetry event.
                # Print a stderr warning that names the unknown entries and
                # the closest registry keys so the user can spot typos
                # without grepping the events log.
                _emit_unknown_analyzer_warning(unknown_names)
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
