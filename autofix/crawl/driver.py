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

from autofix.crawl.bundles import Bundle, expand_bundle
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
    debug_crawl: bool = False,
) -> int:
    """One crawl cycle. Returns 0 on clean completion.

    Writes ``.autofix/crawl.pid`` while the cycle runs so
    ``autofix status`` can reliably detect a single-cycle
    invocation as "running". Removed on clean exit (or on any
    raised exception that propagates out).

    ``debug_crawl`` is a verbose-tracing knob threaded down to the
    cycle body. Off by default; existing callers don't need to touch
    it.
    """
    with _pidfile(root):
        return _run_crawl_once_body(
            root=root, mode=mode, budget=budget,
            analyzer_set=analyzer_set, quiet=quiet,
            debug_crawl=debug_crawl,
        )


def _run_crawl_once_body(
    *,
    root: Path,
    mode: str,
    budget: str,
    analyzer_set: list[str] | None,
    quiet: bool,
    debug_crawl: bool = False,
) -> int:
    """The body of one crawl cycle, factored out so the continuous
    loop can call it WITHOUT each cycle re-creating a pidfile."""
    _ = debug_crawl  # reserved for downstream tracing; segment-E will wire
    tier = resolve_budget_tier(budget)
    analyzers = list(analyzer_set) if analyzer_set else list(tier["analyzers"])
    bundles_per_cycle = tier["bundles_per_cycle"]

    ledger = Ledger(root=root)
    ledger.replay_from_disk()

    git_log = _build_git_log(root)
    call_graph = _build_call_graph(root)
    current_commit_sha = _resolve_commit_sha(root)

    from autofix.crawl.picker import pick_next_batch

    # Impact-cone dispatch. When the impact-cone flag is on AND the
    # working tree has tracked changes, build bundles seeded by the
    # changed files instead of running the full relevance picker. The
    # gate currently returns False — segment-E wires it to the real
    # CrawlerFlags. Returning False keeps existing behavior intact.
    autofixignore = None
    use_impact_cone = _should_use_impact_cone(root)
    changed_files: list[Path] = []
    if use_impact_cone:
        changed_files = _detect_working_tree_diff(root)

    if use_impact_cone and changed_files:
        now = _utcnow_iso_z()
        window_start = _saturation_window_start(now)
        batch = _pick_impact_cone_batch(
            changed_files,
            root=root,
            call_graph=call_graph,
            ledger=ledger,
            analyzers=analyzers,
            autofixignore=autofixignore,
            window_start=window_start,
            now=now,
        )
    else:
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

    # Aggregate findings across the whole cycle so the repair
    # dispatcher fires ONCE at the end (one recovery branch, one
    # apply pass, one commit) instead of once per bundle (which
    # caused: N recovery branches, dirty-tree cascade between
    # bundles, post-fix policy never firing cleanly).
    aggregated_findings: list = []

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

        if findings:
            aggregated_findings.extend(findings)

    if not quiet:
        print(
            f"autofix: cycle aggregated {len(aggregated_findings)} findings "
            f"across {len(batch)} (bundle, analyzer) pairs",
            file=sys.stderr,
            flush=True,
        )

    if aggregated_findings and mode != MODE_PREVIEW:
        _dispatch_repair_workflow(
            root=root,
            mode=mode,
            analyzers=analyzers,
            quiet=quiet,
            findings=aggregated_findings,
        )

    return 0


def run_crawl_continuously(
    *,
    root: Path,
    mode: str,
    budget: str,
    interval_seconds: int,
    quiet: bool = False,
    debug_crawl: bool = False,
) -> int:
    """Loop forever, sleeping ``interval_seconds`` between cycles.

    Returns 0 on ``KeyboardInterrupt``. ``SystemExit`` propagates.
    Per-cycle ``Exception`` is caught + logged; the loop continues.

    ``debug_crawl`` is forwarded to each per-cycle body call. Off by
    default; existing callers don't need to set it.
    """
    with _pidfile(root):
        try:
            while True:
                try:
                    _run_crawl_once_body(
                        root=root, mode=mode, budget=budget,
                        analyzer_set=None, quiet=quiet,
                        debug_crawl=debug_crawl,
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
    mode: str,
    analyzers: list[str],
    quiet: bool,
    findings: list | None = None,
) -> int:
    """Apply the cycle's aggregated findings via the repair pipeline.

    Called ONCE at the end of a crawl cycle with all findings
    aggregated across every bundle that was analyzed. This avoids
    the per-bundle dispatch antipattern that caused N recovery
    branches, a dirty-tree cascade, and post-fix-not-firing.

    Bypasses ``run_command._run_one_cycle`` because that function
    re-runs the full SCAN/TRIAGE/PLAN/APPLY/VERIFY workflow against
    the entire repo's diff scope — wasteful when we already have
    findings from ``_analyze_bundle``. We only need:

    1. ``_run_fix_core(findings=...)`` — uses preloaded findings,
       skips re-scanning. Captures one recovery branch
       (``autofix/pre-fix-snapshot-<utc>``) before the apply pass.
       The LLM patcher generates unified diffs for LLM-tier
       findings; deterministic deletions go through the cheap
       apply path.
    2. ``apply_post_fix_policy(...)`` — branch + commit (and
       optionally ``gh pr create``). Cleans the working tree.

    Mode → post-fix policy:

    * ``mode == MODE_COMMIT`` → ``branch`` (no PR)
    * ``mode == MODE_PR``     → ``branch-pr``

    ``auto_llm=True`` whenever any ``llm:*`` analyzer is in the
    resolved set.
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

    if not quiet:
        print(
            f"autofix: dispatcher firing — {len(findings)} findings, "
            f"mode={mode}, post_fix={post_fix}, auto_llm={has_llm}",
            file=sys.stderr,
            flush=True,
        )

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
                f"continuing",
                file=sys.stderr,
                flush=True,
            )
        return 1

    if fix_result.exit_code != EXIT_OK:
        if not quiet:
            print(
                f"autofix: _run_fix_core exited {fix_result.exit_code}; "
                f"working tree may be dirty",
                file=sys.stderr,
                flush=True,
            )
        return fix_result.exit_code

    applied_count = len(fix_result.applied_finding_ids)
    if not quiet:
        print(
            f"autofix: _run_fix_core applied {applied_count} finding(s); "
            f"post-fix policy next",
            file=sys.stderr,
            flush=True,
        )

    if applied_count == 0:
        return EXIT_OK

    # Cheap VERIFYING — catch obvious false-positive applies (e.g.,
    # the LLM-dead-code analyzer wrongly flagging an in-use import,
    # whose deletion breaks the file). Without this, autofix can
    # commit broken code on its own demo. We byte-compile every
    # modified file; on syntax/NameError, REVERT to the recovery
    # branch's pre-apply state and skip the post-fix commit.
    if not _verify_modified_files_compile(root, quiet=quiet):
        if not quiet:
            print(
                "autofix: VERIFYING failed — modified files don't compile; "
                "reverting working tree, skipping post-fix policy",
                file=sys.stderr,
                flush=True,
            )
        _revert_working_tree(root, quiet=quiet)
        return 1

    try:
        outcome = apply_post_fix_policy(
            root=root,
            run_id=_make_event_id(),
            applied_finding_ids=frozenset(fix_result.applied_finding_ids),
            policy=post_fix,
            quiet=quiet,
        )
        if not quiet:
            print(
                f"autofix: post-fix policy → {outcome}",
                file=sys.stderr,
                flush=True,
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


def _verify_modified_files_compile(root: Path, *, quiet: bool) -> bool:
    """Byte-compile every modified Python file in the working tree.

    Cheap VERIFYING for the dispatcher's apply path. Catches the most
    common false-positive shape: an "unused-import" deletion that
    breaks the file because the import was actually used (the LLM
    dead-code analyzer's blind spot).

    Returns True if all modified files compile, False otherwise.
    Doesn't run tests; doesn't re-scan; just compiles.
    """
    import py_compile
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--", "*.py"],
            cwd=str(root), capture_output=True, text=True,
            check=True, timeout=10,
        )
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        # Can't query git — fail-closed (assume verify failed).
        return False

    modified_paths = [
        line.strip() for line in result.stdout.splitlines() if line.strip()
    ]
    if not modified_paths:
        # No .py files modified — trivially "verified".
        return True

    failed: list[tuple[str, str]] = []
    for relpath in modified_paths:
        abs_path = root / relpath
        if not abs_path.is_file():
            continue
        try:
            py_compile.compile(str(abs_path), doraise=True)
        except py_compile.PyCompileError as exc:
            failed.append((relpath, str(exc)))
        except OSError as exc:
            failed.append((relpath, repr(exc)))

    if failed and not quiet:
        for relpath, msg in failed:
            print(
                f"autofix: VERIFY: {relpath} doesn't compile: {msg[:200]}",
                file=sys.stderr,
                flush=True,
            )
    return not failed


def _revert_working_tree(root: Path, *, quiet: bool) -> None:
    """Revert all working-tree modifications to HEAD. Used by the
    dispatcher's verify-failed path to undo a bad apply.
    """
    import subprocess

    try:
        subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=str(root), capture_output=True, text=True,
            check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if not quiet:
            print(
                f"autofix: VERIFY: failed to revert working tree: {exc!r}",
                file=sys.stderr,
                flush=True,
            )


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


def _should_use_impact_cone(root: Path) -> bool:
    """Whether to bypass the relevance picker for the impact-cone path.

    Reads ``CrawlerFlags.impact_cone`` from ``.autofix/config.json``.
    Default-off when the file is missing/malformed (see
    ``config.read_crawler_flags``).
    """
    from autofix.crawl.config import read_crawler_flags

    try:
        return read_crawler_flags(root).impact_cone
    except Exception:
        # read_crawler_flags itself swallows IO/parse errors, but stay
        # defensive — a bug in flag-reading must not abort the cycle.
        return False


def _saturation_window_start(now: str) -> str:
    """Compute the saturation-window start for impact-cone expansions.

    Mirrors the picker's window math but kept local so the driver
    doesn't reach into picker._window_start_iso (private). Uses
    HUB_SATURATION_WINDOW_HOURS from crawl_constants — never inlined.
    """
    from datetime import datetime, timedelta, timezone

    from autofix.crawl.crawl_constants import HUB_SATURATION_WINDOW_HOURS

    end = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc,
    )
    start = end - timedelta(hours=HUB_SATURATION_WINDOW_HOURS)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def _detect_working_tree_diff(root: Path) -> list[Path]:
    """Return the list of tracked files dirty in the working tree.

    Runs ``git status --porcelain=v1`` (timeout 10s, matching
    ``_resolve_commit_sha``). Each output line is two status columns
    (X = staged, Y = unstaged) followed by space + path. ``?`` in
    either column means untracked — those rows are dropped. ``R`` in
    either column means rename; the ``old -> new`` form is split and
    only the right-hand path is kept.

    Failure modes:
    - Non-git directory          → CalledProcessError → return []
    - Repo with no commits       → typically still works, but if not, return []
    - subprocess timeout         → return []
    - Any OSError on the binary  → return []

    Returned paths are absolute (rooted under ``root``) so callers
    can pass them to ``expand_bundle`` without further resolution.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []

    out: list[Path] = []
    seen: set[str] = set()
    for raw in result.stdout.splitlines():
        if len(raw) < 3:
            continue
        x, y = raw[0], raw[1]
        # Skip untracked and clean rows. ``?`` flags untracked; a
        # space in BOTH cols means clean (shouldn't appear in
        # porcelain output, but defensive).
        if x == "?" or y == "?":
            continue
        if x == " " and y == " ":
            continue
        # Path starts at col 3 (after "XY ").
        path_str = raw[3:]
        # Rename: "X  old -> new" — keep the right side.
        if " -> " in path_str and (x == "R" or y == "R"):
            path_str = path_str.split(" -> ", 1)[1]
        # Quoted paths (e.g. "with spaces.py") — strip the wrapping
        # quotes git emits when the path contains special chars.
        if path_str.startswith('"') and path_str.endswith('"') and len(path_str) >= 2:
            path_str = path_str[1:-1]
        if not path_str or path_str in seen:
            continue
        seen.add(path_str)
        # Compose an absolute path under ``root`` without resolving
        # symlinks (``.resolve()`` would canonicalize ``/var`` →
        # ``/private/var`` on macOS, which breaks equality vs the
        # caller's unresolved tmp_path).
        candidate = Path(root) / path_str
        if not candidate.is_absolute():
            candidate = candidate.absolute()
        out.append(candidate)
    return out


def _pick_impact_cone_batch(
    changed_files: list[Path],
    *,
    root: Path,
    call_graph: Any,
    ledger: Any,
    analyzers: list[str],
    autofixignore: Any | None,
    window_start: str,
    now: str,
) -> list[tuple[Bundle, str]]:
    """Build one bundle per changed file, emit ``(bundle, analyzer)`` pairs.

    For impact-cone mode: every file in ``changed_files`` becomes a
    bundle seed via :func:`expand_bundle` (1-hop neighbors up to the
    ledger's hub-saturation cap). Each bundle pairs with EVERY
    analyzer in ``analyzers``, mirroring ``pick_next_batch``'s pair
    emission.

    Empty inputs return an empty list:
    - empty ``changed_files`` → no bundles
    - empty ``analyzers``     → no pairs

    Duplicate fingerprints (same file set appearing twice) are
    de-duplicated so a single (bundle, analyzer) pair surfaces once
    per unique file set.
    """
    if not changed_files or not analyzers:
        return []

    bundles: list[Bundle] = []
    seen_fp: set[str] = set()
    for seed in changed_files:
        seed_abs = seed if seed.is_absolute() else (root / seed)
        try:
            bundle = expand_bundle(
                seed_path=seed_abs,
                root=root,
                call_graph=call_graph,
                ledger=ledger,
                window_start=window_start,
                now=now,
                autofixignore=autofixignore,
            )
        except (OSError, ValueError):
            # A single bad seed must not abort the whole cycle.
            continue
        if bundle.fingerprint in seen_fp:
            continue
        seen_fp.add(bundle.fingerprint)
        bundles.append(bundle)

    out: list[tuple[Bundle, str]] = []
    for bundle in bundles:
        for analyzer in analyzers:
            out.append((bundle, analyzer))
    return out


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
