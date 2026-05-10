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

from autofix.analyzers import analyze_files
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
    from autofix.crawl.crawl_observability import CycleStats, emit_cycle_stats

    stats = CycleStats()
    tier = resolve_budget_tier(budget)
    analyzers = list(analyzer_set) if analyzer_set else list(tier["analyzers"])
    bundles_per_cycle = tier["bundles_per_cycle"]

    ledger = Ledger(root=root)
    ledger.replay_from_disk()

    git_log = _build_git_log(root)
    call_graph = _build_call_graph(root, git_log)
    current_commit_sha = _resolve_commit_sha(root)

    from autofix.crawl.autofixignore import AutofixIgnore
    from autofix.crawl.bundles import ClassAwareConfig
    from autofix.crawl.config import read_crawler_flags
    from autofix.crawl.file_classifier import load_console_script_paths
    from autofix.crawl.picker import pick_next_batch
    from autofix.crawl.score import ScoringFlags

    # Resolve the four operator-tunable feature surfaces from
    # ``.autofix/config.json`` once per cycle. Each is byte-identity-
    # safe at the default (all flags False / autofixignore absent / no
    # console-scripts) — the picker / expand_bundle short-circuits
    # back to the legacy code path.
    crawler_flags = read_crawler_flags(root)
    autofixignore = AutofixIgnore.load(root)
    console_script_paths = load_console_script_paths(root)
    scoring_flags: ScoringFlags | None = (
        ScoringFlags(
            entrypoint_boost=crawler_flags.entrypoint_boost,
            low_value_class_penalty=crawler_flags.low_value_class_penalty,
            oversize_file_penalty=crawler_flags.oversize_file_penalty,
        )
        if (
            crawler_flags.entrypoint_boost
            or crawler_flags.low_value_class_penalty
            or crawler_flags.oversize_file_penalty
        )
        else None
    )
    class_aware_config: ClassAwareConfig | None = (
        ClassAwareConfig(root=root) if crawler_flags.class_aware else None
    )

    # Impact-cone dispatch. When the impact-cone flag is on AND the
    # working tree has tracked changes, build bundles seeded by the
    # changed files instead of running the full relevance picker.
    use_impact_cone = crawler_flags.impact_cone
    changed_files: list[Path] = []
    if use_impact_cone:
        changed_files = _detect_working_tree_diff(root)

    if use_impact_cone and changed_files:
        now = _utcnow_iso_z()
        window_start = _saturation_window_start(now)
        bundles = _pick_impact_cone_batch(
            changed_files,
            root=root,
            call_graph=call_graph,
            ledger=ledger,
            autofixignore=autofixignore,
            window_start=window_start,
            now=now,
        )
    else:
        bundles = pick_next_batch(
            root=root,
            ledger=ledger,
            current_commit_sha=current_commit_sha,
            git_log=git_log,
            call_graph=call_graph,
            bundles_per_cycle=bundles_per_cycle,
            autofixignore=autofixignore,
            scoring_flags=scoring_flags,
            class_aware_config=class_aware_config,
            console_script_paths=console_script_paths,
        )

    # Cross bundles × analyzers here in the consumer — the crawler
    # is analyzer-agnostic. One (bundle, analyzer) pair per analyzer
    # per bundle.
    batch: list[tuple[Bundle, str]] = [
        (bundle, analyzer) for bundle in bundles for analyzer in analyzers
    ]

    # Populate the basic stats fields. Score-breakdown / per-filter
    # counters (junk_sinks, autofixignore, class, size_cap,
    # budget_hits) require deeper instrumentation in pick_next_batch
    # + expand_bundle and remain zero for now — the operator still
    # gets bundle counts, byte distribution, and seed list.
    seen_seeds: list[str] = []
    for bundle in bundles:
        seed_str = str(bundle.seed_path)
        if seed_str not in seen_seeds:
            seen_seeds.append(seed_str)
        stats.bundle_size_bytes_list.append(bundle.total_bytes)
    stats.bundles_built = len(seen_seeds)
    stats.top_seeds = seen_seeds[:10]

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
        findings = analyze_files(
            bundle.file_paths,
            analyzers=[analyzer],
            repo_root=root,
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

    # Emit per-cycle stats. quiet=True suppresses everything;
    # debug_crawl=True emits the full breakdown; otherwise a
    # single-line INFO summary lands on stderr.
    emit_cycle_stats(stats, quiet=quiet, debug_crawl=debug_crawl)

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
    findings from ``analyze_files``. We only need:

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
    """Cheap-VERIFY pass over every modified Python file in the working tree.

    Two checks, in order. Both must pass; either failing fails VERIFY.

    1. Byte-compile via ``py_compile``. Catches syntax / import-time
       errors. The most common false-positive shape it catches: an
       "unused-import" deletion that breaks the file because the
       import was actually used (the LLM dead-code analyzer's blind
       spot).

    2. ``mypy --warn-unreachable`` on the modified files. Catches the
       LLM-patcher's defensive-dead-code shape: e.g. ``getattr(obj,
       "field_that_does_not_exist", None)`` that's always None,
       making the guarded branch unreachable. Surfaced by 2026-05-09
       PR #87 — a patch that compiled cleanly and applied cleanly
       but added 11 lines of unreachable code.

    Returns True if all modified files pass both checks, False
    otherwise. Doesn't run tests; doesn't re-scan.
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

    # --- Check 1: byte-compile ----------------------------------
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
    if failed:
        return False

    # --- Check 2: mypy --warn-unreachable -----------------------
    # We deliberately invoke mypy as a subprocess (not as a library
    # API) so a mypy crash, missing-stub, or import error degrades
    # this pass into "no unreachable findings" rather than aborting
    # the cycle. We only fail VERIFY on the literal substring
    # ``Statement is unreachable`` which is what mypy emits under
    # ``--warn-unreachable``. Other mypy errors (unrelated type
    # mismatches, missing imports, etc.) are NOT enforced here —
    # this is a targeted dead-code gate, not a full type check.
    if not _check_no_unreachable_branches(
        root, modified_paths, quiet=quiet,
    ):
        return False

    return True


def _check_no_unreachable_branches(
    root: Path, modified_paths: list[str], *, quiet: bool,
) -> bool:
    """Return False iff mypy --warn-unreachable flagged at least one
    of the modified Python files. Soft on every other failure mode.

    A mypy import / config / crash error is treated as "could not
    analyze" and returns True (no unreachable found). The intent is
    a narrow gate against the specific failure shape — a patch that
    introduces a definitely-dead branch — without making mypy a
    hard dependency of the crawl pipeline.
    """
    import shutil
    import subprocess

    mypy_bin = shutil.which("mypy")
    if mypy_bin is None:
        # Mypy not available in this environment. Fail-soft: the
        # primary byte-compile gate already passed.
        return True

    args = [
        mypy_bin,
        "--warn-unreachable",
        "--no-error-summary",
        "--hide-error-codes",
        "--no-incremental",
        "--ignore-missing-imports",
        *modified_paths,
    ]
    try:
        proc = subprocess.run(
            args, cwd=str(root), capture_output=True, text=True,
            timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True

    output = (proc.stdout or "") + (proc.stderr or "")
    unreachable_lines = [
        line for line in output.splitlines()
        if "Statement is unreachable" in line
        or "Right operand of" in line and "always" in line
    ]
    if unreachable_lines and not quiet:
        print(
            "autofix: VERIFY: mypy --warn-unreachable flagged "
            f"{len(unreachable_lines)} dead branch(es) in modified files:",
            file=sys.stderr,
            flush=True,
        )
        for line in unreachable_lines[:10]:
            print(f"autofix: VERIFY:   {line}", file=sys.stderr, flush=True)
    return not unreachable_lines


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

    A duck-typed object exposing ``list_candidate_files``,
    ``days_since_last_commit``, ``commits_in_last_30_days``.
    Backed by ``git`` subprocess calls; falls back to ``Path.rglob``
    for non-git trees.

    The adapter is language-agnostic — ``list_candidate_files``
    returns every tracked path. Downstream analyzers decide what to
    do with each file (Python AST analyzers no-op on non-Python;
    LLM analyzers handle whatever they're given).
    """
    return _GitLogAdapter(root)


class _NoNeighbors:
    """Fall-soft adapter — returns empty neighbors for any path.

    Used by ``_build_call_graph`` when ``CallGraph.build_from_root``
    fails (missing SCIP shards, IO errors, etc.). The bundle
    expander degrades to singleton bundles in this mode — the
    crawler keeps running, but cross-file value is suspended until
    the indexer is repaired.
    """

    def neighbors_of(self, path: Path) -> list[Path]:
        return []


def _build_call_graph(root: Path, git_log: Any | None = None) -> Any:
    """Build a path-level neighbor adapter for the bundle expander.

    Two independent neighbor signals, both fall-soft and additive:

    1. **SCIP call graph** — symbol-level via
       :class:`autofix.invalidation.call_graph.CallGraph`.
       Precise but covers only languages with a SCIP indexer
       wired in (Python, TS/JS, Go).
    2. **Text-reference index** — language-agnostic basename
       references via
       :func:`autofix.crawl._text_reference_index.build_text_reference_indexes`.
       Fuzzy but works for Dart/HTML/anything tracked by git.

    Both feed into :class:`CallGraphPathAdapter`, which unions
    them per ``neighbors_of(path)`` lookup. If both signals fail
    to build, returns a :class:`_NoNeighbors` sentinel so bundles
    degrade to singletons without aborting the cycle.

    ``except Exception`` on each signal is intentional — daemon
    survival outranks fault diagnosis at this layer. Build-failure
    modes seen in the wild include ImportError if SCIP isn't
    installed, OSError for filesystem issues, ValueError for
    malformed cache shards, and IO errors when reading non-UTF8
    files for the text index.
    """
    cg: Any | None = None
    try:
        from autofix.invalidation.call_graph import CallGraph

        cg = CallGraph.build_from_root(root)
    except Exception:
        cg = None

    text_in: dict[str, Any] | None = None
    text_out: dict[str, Any] | None = None
    try:
        from autofix.crawl._text_reference_index import (
            build_text_reference_indexes,
        )

        candidates_source = git_log if git_log is not None else _GitLogAdapter(root)
        candidates = list(candidates_source.list_candidate_files())
        text_in, text_out = build_text_reference_indexes(root, candidates)
    except Exception:
        text_in = None
        text_out = None

    if cg is None and not text_in and not text_out:
        return _NoNeighbors()

    from autofix.crawl._call_graph_adapter import CallGraphPathAdapter

    return CallGraphPathAdapter(
        cg,
        root=root,
        text_incoming=text_in,
        text_outgoing=text_out,
    )


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
    autofixignore: Any | None,
    window_start: str,
    now: str,
) -> list[Bundle]:
    """Build one bundle per changed file. Returns deduped bundles.

    For impact-cone mode: every file in ``changed_files`` becomes a
    bundle seed via :func:`expand_bundle` (1-hop neighbors up to the
    ledger's hub-saturation cap). Mirrors ``pick_next_batch``'s
    analyzer-agnostic shape — the caller crosses bundles with
    analyzers afterwards.

    Empty input → empty list.

    Duplicate fingerprints (same file set appearing twice) are
    de-duplicated.
    """
    if not changed_files:
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

    return bundles


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
    """git-subprocess-backed adapter used by the picker.

    Recency and churn are computed via two batch ``git log`` calls
    (one per cycle, lazily on first lookup) and cached as
    ``{relpath: value}`` dicts. Per-file lookups are then O(1) dict
    hits — no subprocess fan-out, no quadratic cycle cost.

    Language-agnostic: ``list_candidate_files`` returns every
    tracked path. The crawler hands whatever it picks to the
    analyzer pipeline; analyzers decide whether they can do
    something with it.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._days_cache: dict[str, int] | None = None
        self._churn_cache: dict[str, int] | None = None
        self._days_loaded = False
        self._churn_loaded = False

    def list_candidate_files(self) -> list[Path]:
        import subprocess
        try:
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=str(self._root), capture_output=True, text=True,
                check=True, timeout=30,
            )
            return [
                self._root / line.strip()
                for line in result.stdout.splitlines()
                if line.strip()
            ]
        except (subprocess.CalledProcessError, OSError):
            return [
                p for p in self._root.rglob("*")
                if p.is_file() and not _has_hidden_component(p, self._root)
            ]

    def _path_key(self, path: Path | str) -> str:
        p = Path(path) if not isinstance(path, Path) else path
        if p.is_absolute():
            try:
                return str(p.relative_to(self._root))
            except ValueError:
                return str(p)
        return str(p)

    def _ensure_days_cache(self) -> None:
        if self._days_loaded:
            return
        self._days_loaded = True
        import subprocess
        import time
        cache: dict[str, int] = {}
        try:
            result = subprocess.run(
                ["git", "log", "--name-only", "--pretty=format:__C__%ct"],
                cwd=str(self._root), capture_output=True, text=True,
                check=True, timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            self._days_cache = None
            return
        now = int(time.time())
        cur_ts: int | None = None
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("__C__"):
                try:
                    cur_ts = int(line[5:])
                except ValueError:
                    cur_ts = None
                continue
            if cur_ts is None or line in cache:
                continue
            cache[line] = max(0, (now - cur_ts) // 86400)
        self._days_cache = cache

    def _ensure_churn_cache(self) -> None:
        if self._churn_loaded:
            return
        self._churn_loaded = True
        import subprocess
        cache: dict[str, int] = {}
        try:
            result = subprocess.run(
                ["git", "log", "--since=30.days.ago", "--name-only", "--pretty=format:__C__"],
                cwd=str(self._root), capture_output=True, text=True,
                check=True, timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            self._churn_cache = None
            return
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if not line or line == "__C__":
                continue
            cache[line] = cache.get(line, 0) + 1
        self._churn_cache = cache

    def days_since_last_commit(self, path: Path) -> int | None:
        self._ensure_days_cache()
        if self._days_cache is None:
            return None
        return self._days_cache.get(self._path_key(path))

    def commits_in_last_30_days(self, path: Path) -> int | None:
        self._ensure_churn_cache()
        if self._churn_cache is None:
            return None
        return self._churn_cache.get(self._path_key(path), 0)


def _has_hidden_component(path: Path, root: Path) -> bool:
    """True if any path component (relative to root) starts with '.'.

    Used by the rglob fallback in ``_GitLogAdapter.list_candidate_files``
    to skip ``.git/``, ``.venv/``, ``__pycache__`` (the leading
    underscore variant is handled separately if needed) and any other
    hidden directory git would have skipped via ``ls-files``. Without
    this, the non-git fallback would enumerate ``.git/objects/*`` and
    flood the picker with thousands of bogus candidates.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(part.startswith(".") for part in rel.parts)


__all__ = [
    "run_crawl_once",
    "run_crawl_continuously",
]
