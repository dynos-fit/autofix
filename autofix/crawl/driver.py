"""Crawl driver — one-cycle and continuous-loop entry points (ARCH-016).

* :func:`run_crawl_once` — runs ONE crawl cycle and returns an exit
  code. The cycle replays the ledger, picks the next batch, runs
  analyzers per (bundle, analyzer) pair, records ledger rows, and
  optionally dispatches to the existing ``_run_one_cycle`` workflow
  body when ``mode != preview`` and findings exist.
* :func:`run_crawl_continuously` — loops forever, sleeping
  ``interval_seconds`` between cycles. Catches per-cycle
  ``Exception`` and continues; ``KeyboardInterrupt`` and
  ``SystemExit`` propagate. Maintains a pidfile at
  ``.autofix/crawl.pid`` for the ``status`` command to read.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path
from typing import Any

from autofix.crawl.config import resolve_budget_tier
from autofix.crawl.crawl_constants import (
    MODE_PREVIEW,
)
from autofix.crawl.ledger import Ledger, LedgerRow


_PIDFILE_NAME = "crawl.pid"


@contextlib.contextmanager
def _pidfile(root: Path):
    """Context manager that writes ``.autofix/crawl.pid`` on enter,
    removes it on exit. Used by BOTH ``run_crawl_once`` and
    ``run_crawl_continuously`` so ``autofix status`` can reliably
    detect a running daemon regardless of which entry point is in
    use.

    Best-effort cleanup: if the pidfile was deleted out from under
    us (or a permission error blocks unlink), the cleanup swallows
    the error rather than propagating.
    """
    path = root / ".autofix" / _PIDFILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))
    try:
        yield path
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def run_crawl_once(
    *,
    root: Path,
    mode: str,
    budget: str,
    analyzer_set: list[str] | None = None,
    quiet: bool = False,
) -> int:
    """One crawl cycle. Returns 0 on clean completion.

    Writes ``.autofix/crawl.pid`` while the cycle runs so
    ``autofix status`` can reliably detect a single-cycle
    invocation as "running". Removed on clean exit (or on any
    raised exception that propagates out).
    """
    with _pidfile(root):
        return _run_crawl_once_body(
            root=root, mode=mode, budget=budget,
            analyzer_set=analyzer_set, quiet=quiet,
        )


def _run_crawl_once_body(
    *,
    root: Path,
    mode: str,
    budget: str,
    analyzer_set: list[str] | None,
    quiet: bool,
) -> int:
    """The body of one crawl cycle, factored out so the continuous
    loop can call it WITHOUT each cycle re-creating a pidfile."""
    tier = resolve_budget_tier(budget)
    analyzers = list(analyzer_set) if analyzer_set else list(tier["analyzers"])
    bundles_per_cycle = tier["bundles_per_cycle"]

    ledger = Ledger(root=root)
    ledger.replay_from_disk()

    git_log = _build_git_log(root)
    call_graph = _build_call_graph(root)
    current_commit_sha = _resolve_commit_sha(root)

    from autofix.crawl.picker import pick_next_batch

    batch = pick_next_batch(
        root=root,
        ledger=ledger,
        current_commit_sha=current_commit_sha,
        git_log=git_log,
        call_graph=call_graph,
        analyzers=analyzers,
        bundles_per_cycle=bundles_per_cycle,
    )

    if not quiet:
        print(
            f"autofix: cycle picked {len(batch)} (bundle, analyzer) pairs",
            file=sys.stderr,
            flush=True,
        )

    for bundle, analyzer in batch:
        findings = _analyze_bundle(
            bundle=bundle,
            analyzer=analyzer,
            root=root,
            commit_sha=current_commit_sha,
        )

        ledger.record(LedgerRow(
            ts=_utcnow_iso_z(),
            bundle_fingerprint=bundle.fingerprint,
            seed_path=str(bundle.seed_path),
            file_paths=tuple(str(p) for p in bundle.file_paths),
            analyzer=analyzer,
            last_commit_sha=current_commit_sha,
            last_finding_count=len(findings),
            cache_hit=False,
            event_id=_make_event_id(),
        ))

        if findings and mode != MODE_PREVIEW:
            _dispatch_repair_workflow(
                root=root, bundle=bundle, mode=mode,
                analyzers=analyzers, quiet=quiet,
                findings=findings,
            )

    return 0


def run_crawl_continuously(
    *,
    root: Path,
    mode: str,
    budget: str,
    interval_seconds: int,
    quiet: bool = False,
) -> int:
    """Loop forever, sleeping ``interval_seconds`` between cycles.

    Returns 0 on ``KeyboardInterrupt``. ``SystemExit`` propagates.
    Per-cycle ``Exception`` is caught + logged; the loop continues.
    """
    with _pidfile(root):
        try:
            while True:
                try:
                    _run_crawl_once_body(
                        root=root, mode=mode, budget=budget,
                        analyzer_set=None, quiet=quiet,
                    )
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    if not quiet:
                        print(
                            f"autofix: cycle raised {exc!r}; continuing",
                            file=sys.stderr,
                            flush=True,
                        )
                _sleep(interval_seconds)
        except KeyboardInterrupt:
            return 0


# --- Internal helpers ------------------------------------------------------


def _sleep(seconds: int) -> None:
    """Indirected so tests can patch sleep without slowing themselves."""
    time.sleep(seconds)


def _analyze_bundle(
    *, bundle: Any, analyzer: str, root: Path, commit_sha: str
) -> list:
    """Run one analyzer against every file in the bundle.

    Looks up the analyzer's callable in
    :data:`autofix.funnel.pipeline._ANALYZER_REGISTRY` and invokes
    it once per file in the bundle (each call gets that file's
    ``ParseResult`` + ``SymbolTable``). Returns the union of
    findings across all files. The LLM cache from the analyzer's
    base class deduplicates re-scans of unchanged files
    automatically — same prompt + same commit_sha + same model
    means a cache hit, free.

    OSError / parse-failure on any single file is swallowed so a
    bad file doesn't abort the cycle; the rest of the bundle still
    contributes findings.
    """
    from autofix.funnel.pipeline import _ANALYZER_REGISTRY
    from autofix.indexing.symbols import build_symbol_table
    from autofix.parsing.tree_sitter import parse_file

    callable_ = _ANALYZER_REGISTRY.get(analyzer)
    if callable_ is None:
        return []

    findings: list = []
    for path in bundle.file_paths:
        try:
            parse_result = parse_file(path, repo_root=root)
        except (FileNotFoundError, PermissionError, OSError):
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
        except Exception as exc:  # noqa: BLE001 — last-resort safety net
            # Any other analyzer failure (subprocess timeout, network,
            # parse-tree edge case) is logged and skipped. A single
            # bad file must not abort a long-running crawl cycle.
            print(
                f"autofix: warning: {analyzer} on {path} failed: "
                f"{type(exc).__name__}: {exc!r}; continuing",
                file=sys.stderr,
                flush=True,
            )
            continue
    return findings


def _dispatch_repair_workflow(
    *,
    root: Path,
    bundle: Any,
    mode: str,
    analyzers: list[str],
    quiet: bool,
    findings: list | None = None,
) -> int:
    """Apply the bundle's findings via the repair pipeline.

    Bypasses ``run_command._run_one_cycle`` because that function
    re-runs the full SCAN/TRIAGE/PLAN/APPLY/VERIFY workflow against
    the entire repo's diff scope — which can be hundreds of files
    × N analyzers = thousands of LLM calls. The crawl already
    analyzed the bundle and produced findings; we only need to:

    1. Apply via ``_run_fix_core(findings=...)`` — uses
       preloaded findings, skips re-scanning. The LLM patcher
       generates unified diffs for LLM-tier findings.
    2. Run the post-fix policy to branch + commit (and optionally
       open a PR).

    Maps the crawl's mode to the post-fix policy:

    * ``mode == MODE_COMMIT`` → ``branch`` (no PR)
    * ``mode == MODE_PR``     → ``branch-pr``

    The ``--auto-llm`` flag is set whenever any ``llm:*`` analyzer
    is in the resolved set — signals "this cycle wants LLM-generated
    patches".
    """
    from autofix.cli.fix_command import _run_fix_core
    from autofix.cli.post_fix_policy import apply_post_fix_policy
    from autofix.cli.run_constants import EXIT_OK
    from autofix.crawl.crawl_constants import MODE_PR

    if findings is None:
        findings = []
    if not findings:
        return EXIT_OK

    has_llm = any(a.startswith("llm:") for a in analyzers)
    post_fix = "branch-pr" if mode == MODE_PR else "branch"

    try:
        fix_result = _run_fix_core(
            root=root,
            findings=findings,
            apply_mode=True,
            suggest_mode=False,
            auto_llm=has_llm,
            force=False,
            max_llm_patches=None,
            recovery_branch_already_captured=False,
            quiet=quiet,
        )
    except Exception as exc:
        if not quiet:
            print(
                f"autofix: repair workflow raised {exc!r}; "
                f"continuing with the next bundle",
                file=sys.stderr,
                flush=True,
            )
        return 1

    if fix_result.exit_code != EXIT_OK:
        return fix_result.exit_code

    if not fix_result.applied_finding_ids:
        return EXIT_OK

    try:
        apply_post_fix_policy(
            root=root,
            run_id=_make_event_id(),
            applied_finding_ids=frozenset(fix_result.applied_finding_ids),
            policy=post_fix,
            quiet=quiet,
        )
    except Exception as exc:
        if not quiet:
            print(
                f"autofix: post-fix policy raised {exc!r}; "
                f"working tree may be dirty",
                file=sys.stderr,
                flush=True,
            )
        return 1

    return EXIT_OK


def _build_git_log(root: Path) -> Any:
    """Build a minimal git_log adapter shape used by the picker.

    For now: a duck-typed object exposing ``list_python_files``,
    ``days_since_last_commit``, ``commits_in_last_30_days``,
    ``import_fanout``. Backed by ``git`` subprocess calls; falls
    back to ``Path.rglob`` for non-git trees.
    """
    return _GitLogAdapter(root)


def _build_call_graph(root: Path) -> Any:
    """Build a call-graph adapter exposing ``neighbors_of(path) ->
    list[Path]``.

    Stub for v1: returns no neighbors for any path. The bundle
    expansion still works (singletons bundles), and ARCH-013/014
    LLM analyzers still get cross-file context via the real
    ``invalidation.planner.plan`` once it's wired in a follow-up.
    """
    class _NoNeighbors:
        def neighbors_of(self, path: Path) -> list[Path]:
            return []
    return _NoNeighbors()


def _resolve_commit_sha(root: Path) -> str:
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True, text=True, check=True,
            timeout=10,
        )
        return result.stdout.strip() or "_no_commit"
    except (subprocess.CalledProcessError, OSError):
        return "_no_commit"


def _utcnow_iso_z() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_event_id() -> str:
    import secrets
    return f"evt_{secrets.token_urlsafe(8)}"


class _GitLogAdapter:
    """Minimal git_log adapter used by the picker."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def list_python_files(self) -> list[Path]:
        import subprocess
        try:
            result = subprocess.run(
                ["git", "ls-files", "*.py"],
                cwd=str(self._root), capture_output=True, text=True,
                check=True, timeout=30,
            )
            return [
                self._root / line.strip()
                for line in result.stdout.splitlines()
                if line.strip()
            ]
        except (subprocess.CalledProcessError, OSError):
            return [p for p in self._root.rglob("*.py") if p.is_file()]

    def days_since_last_commit(self, path: Path) -> int | None:
        return 0

    def commits_in_last_30_days(self, path: Path) -> int | None:
        return 0

    def import_fanout(self, path: Path) -> int | None:
        return 0


__all__ = [
    "run_crawl_once",
    "run_crawl_continuously",
]
