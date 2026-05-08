"""The ``autofix run`` umbrella subcommand (ARCH-010).

Drives the full repair workflow loop — scan → triage → plan →
(optionally) apply → verify → done|retry|human-review — by stitching
together existing primitives (``run_scan`` via ``_run_scan_core``,
``coordinate_repairs``, ``produce_patch``, ``_run_fix_core``) under
the producer-only state machine in :mod:`autofix.workflow.state_machine`.

Each state edge is recorded by calling ``StateMachine.transition(...)``
with a stage-specific evidence hash derived from the canonical-JSON
SHA-256 of the payload that crossed that edge.

All numeric and string constants live in :mod:`autofix.cli.run_constants`
(the "no magic numbers" discipline). This module imports each constant
by explicit name; no inline literals for budget, threshold, exit codes,
recovery-branch prefix/format, or verbose state labels.

Bare invocation (``autofix run --root <path>``) performs SCANNING →
TRIAGING → PLANNING → HUMAN_REVIEW → DONE, exiting with the dedicated
human-review exit code. With ``--apply``, the loop additionally
performs APPLYING → VERIFYING → {DONE | RETRY → TRIAGING (up to
``--max-retries``) | FAILED}. With ``--apply --auto-llm``, a single
recovery branch is captured at the pre-run HEAD via the same pathway
``fix_command`` already uses (one branch per ``autofix run``
invocation, NOT one per APPLYING entry on retries).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from autofix.cli.fix_command import _run_fix_core
from autofix.cli.run_constants import (
    DEFAULT_AUTO_LLM_ANALYZERS,
    DEFAULT_MAX_RETRIES,
    EVIDENCE_PLACEHOLDER,
    EXIT_FAILED,
    EXIT_HUMAN_REVIEW,
    EXIT_OK,
    EXIT_USAGE_ERROR,
    LLM_ANALYZER_PREFIX,
    LLM_PATCH_THRESHOLD,
    STATE_LABEL_VERBOSE,
)
from autofix.cli.scan_command import _run_scan_core
from autofix.repair import RepairTier, coordinate_repairs, produce_patch
from autofix.workflow import State, StateMachine


HELP_DESCRIPTION: str = (
    "Run the full autofix workflow loop: scan, triage, plan, "
    "(optionally) apply, verify, and retry until the patched findings "
    "are gone or --max-retries is exhausted."
)


HELP_EPILOG: str = (
    "Exit-code map:\n"
    "  0 — DONE (workflow converged or no findings to fix)\n"
    "  1 — FAILED (max-retries exhausted, or IO/scan error)\n"
    "  2 — argparse usage error (bad flag combination)\n"
    "  3 — HUMAN_REVIEW (preview-only mode; no source mutation)\n"
    "\n"
    "Bare 'autofix run' is preview-only (exit 3). Add --apply to "
    "actually apply deterministic deletions; add --apply --auto-llm "
    "for LLM-generated patches as well.\n"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``run``'s flags on the given (sub)parser."""
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="Repository root (must be inside a git working tree).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply deterministic deletions. Without this flag, the "
        "workflow stops after PLANNING and exits with the human-review "
        "code.",
    )
    parser.add_argument(
        "--suggest",
        action="store_true",
        help="Print LLM-patch previews to stdout. Mutually exclusive "
        "with --auto-llm.",
    )
    parser.add_argument(
        "--auto-llm",
        action="store_true",
        help="Apply LLM-generated patches in addition to deterministic "
        "deletions. Requires --apply.",
    )
    parser.add_argument(
        "--analyzers",
        type=str,
        default="",
        help="Comma-separated analyzer set (e.g. 'cheap,linter:ruff'). "
        "Empty means: when --auto-llm is set, expand to "
        "'cheap,llm:security,llm:code-quality,llm:dead-code,llm:performance' "
        "(every LLM bug-finder); otherwise only 'cheap' runs.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Maximum VERIFYING→RETRY iterations before giving up "
        f"(default: {DEFAULT_MAX_RETRIES}).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-state progress lines on stderr.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Long-running mode: subscribe to filesystem events under "
        "--root and run one full workflow cycle per Watchman batch. "
        "Reuses the WatcherSession from `autofix watch`. Requires "
        "pywatchman + the watchman daemon.",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Watchman opaque clock string (only meaningful with "
        "--watch). When omitted, the watcher subscribes from now.",
    )
    parser.add_argument(
        "--safety-sweep",
        type=str,
        default=None,
        dest="safety_sweep",
        help="Maximum tolerated staleness for the last full sweep "
        "(only meaningful with --watch), expressed as Nh or Nm "
        "(e.g. '1h', '30m'). When the wall-clock delta exceeds this, "
        "the watcher forces a full-cycle dispatch regardless of the "
        "Watchman event stream.",
    )
    from autofix.cli.post_fix_constants import ALLOWED_POST_FIX
    parser.add_argument(
        "--post-fix",
        type=str,
        default=None,
        dest="post_fix",
        choices=ALLOWED_POST_FIX,
        help="Post-DONE branch-and-commit policy. 'working-tree' "
        "(default) leaves the working tree dirty; 'branch' creates "
        "autofix/fixes-<run-id> + commits the applied fixes; "
        "'branch-pr' additionally runs `gh pr create`. Overrides the "
        "post_fix key in .autofix/config.json.",
    )


def _hash_payload(payload: object) -> str:
    """Return SHA-256 hex of the canonical JSON of ``payload``."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()


def _finding_id(f: object) -> str:
    """Best-effort finding-id extraction from a CandidateFinding-like object."""
    return str(getattr(f, "finding_id", None) or getattr(f, "id", None) or "")


def _dedup_findings(findings: list) -> list:
    """Dedup findings by (path, start_line, end_line) per AC-8.b.

    Mirrors the dedup pass in ``fix_command._run_impl`` (lines 647-658).
    The cheap analyzer emits one finding per name in a multi-name import,
    so ``from x import a, b`` produces two findings sharing the same
    line triple; we collapse them to a single line-level entry before
    routing through ``coordinate_repairs``.
    """
    seen: set[tuple] = set()
    out: list = []
    for f in findings:
        key = (
            getattr(f, "path", None),
            getattr(f, "start_line", None),
            getattr(f, "end_line", None),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _resolve_analyzer_set(args: argparse.Namespace) -> list[str] | None:
    """Resolve the active analyzer set from CLI args.

    Resolution rules (run_command's UX contract):

    - When ``--analyzers`` is set explicitly, parse it verbatim. If
      ``--auto-llm`` is ALSO set but the resolved set has no
      ``llm:*`` analyzer, emit a one-line stderr warning so the
      operator notices the silent footgun. (Behavior unchanged: the
      run still proceeds with whatever set was specified.)
    - When ``--analyzers`` is omitted AND ``--auto-llm`` is set,
      expand the implicit set to
      :data:`DEFAULT_AUTO_LLM_ANALYZERS` so "I asked for LLM-driven
      repair" is a single flag rather than a multi-analyzer
      comma-separated string.
    - Otherwise (the bare ``autofix run`` / ``run --apply`` path),
      return ``None`` — the funnel applies its own
      "cheap-only" default.
    """
    raw_analyzers = (getattr(args, "analyzers", "") or "").strip()
    parts = [p.strip() for p in raw_analyzers.split(",") if p.strip()]
    auto_llm = bool(getattr(args, "auto_llm", False))
    quiet = bool(getattr(args, "quiet", False))

    if parts:
        if auto_llm and not any(p.startswith(LLM_ANALYZER_PREFIX) for p in parts):
            if not quiet:
                print(
                    "autofix: warning: --auto-llm is set but --analyzers "
                    f"({raw_analyzers!r}) has no llm:* analyzer; LLM "
                    "patches will only fire if a non-LLM analyzer's "
                    "finding routes through the LLM tier.",
                    file=sys.stderr,
                    flush=True,
                )
        return parts

    if auto_llm:
        return list(DEFAULT_AUTO_LLM_ANALYZERS)

    return None


def _emit_progress(state: State, *, quiet: bool) -> None:
    """Emit one stderr progress line at transition entry unless --quiet."""
    if quiet:
        return
    label = STATE_LABEL_VERBOSE.get(state, state.value)
    print(f"autofix: {label}...", file=sys.stderr, flush=True)


def _run_one_cycle(
    args: argparse.Namespace,
    analyzer_set: list[str] | None,
    *,
    fresh_instance: bool = False,
) -> int:
    """One full workflow cycle (SCANNING → … → DONE/RETRY/FAILED).

    Extracted from :func:`run` so that ``autofix run --watch``'s
    per-batch dispatcher can invoke the same body verbatim.
    ``fresh_instance`` is propagated to ``_run_scan_core`` so the
    watch path can flag full-sweep cycles distinctly.
    """
    root: Path = args.root
    quiet: bool = bool(args.quiet)

    # State machine — auto-writes initial SCANNING row.
    sm = StateMachine(root=root)

    # --- 1. SCANNING ------------------------------------------------------
    _emit_progress(State.SCANNING, quiet=quiet)
    scan_result = _run_scan_core(
        root=root,
        full_sweep=True,
        analyzer_set=analyzer_set,
        scan_id=None,
        fresh_instance=fresh_instance,
        quiet=quiet,
    )
    if scan_result.exit_code != EXIT_OK:
        sm.transition(
            to_state=State.FAILED,
            evidence_sha256=EVIDENCE_PLACEHOLDER,
            reason=f"scan_failed_exit_{scan_result.exit_code}",
        )
        return EXIT_FAILED

    # AC-8.b: dedup by (path, start_line, end_line) before TRIAGING.
    findings = _dedup_findings(list(scan_result.findings))
    finding_ids = sorted(_finding_id(f) for f in findings)

    # --- 2. TRIAGING ------------------------------------------------------
    _emit_progress(State.TRIAGING, quiet=quiet)
    sm.transition(
        to_state=State.TRIAGING,
        evidence_sha256=_hash_payload(finding_ids),
    )
    tasks = coordinate_repairs(
        findings, threshold=LLM_PATCH_THRESHOLD, root=None
    )
    llm_tasks = [t for t in tasks if t.tier == RepairTier.LLM_PATCH]

    # --- 3. PLANNING ------------------------------------------------------
    _emit_progress(State.PLANNING, quiet=quiet)
    task_ids = sorted(_finding_id(t.finding) for t in llm_tasks)
    sm.transition(
        to_state=State.PLANNING,
        evidence_sha256=_hash_payload(task_ids),
    )
    patches = []
    successful_finding_ids: list[str] = []
    for task in llm_tasks:
        patch = produce_patch(task, root=root)
        if patch is not None:
            patches.append(patch)
            successful_finding_ids.append(_finding_id(task.finding))
    patched_ids = sorted(successful_finding_ids)

    # --- 4. APPLYING / HUMAN_REVIEW ---------------------------------------
    if not args.apply:
        # Bare run / --suggest: no source mutation. Per AC-9, the
        # PLANNING→HUMAN_REVIEW edge carries the sorted patched-finding-id
        # payload (NOT EVIDENCE_PLACEHOLDER — that's reserved for FAILED
        # transitions and the SCANNING→FAILED edge where no payload exists).
        _emit_progress(State.HUMAN_REVIEW, quiet=quiet)
        sm.transition(
            to_state=State.HUMAN_REVIEW,
            evidence_sha256=_hash_payload(patched_ids),
            reason="preview_only",
        )
        return EXIT_HUMAN_REVIEW

    # --- 5. APPLY/VERIFY/RETRY loop --------------------------------------
    applied_finding_ids: set[str] = set()
    recovery_branch_captured = False
    attempt = 0
    # AC-9: PLANNING→APPLYING evidence carries sorted patched_ids
    # (finding-ids whose produce_patch returned non-None). The retry
    # loop's later APPLYING entries hash the post-coordinator task list
    # for that iteration.
    applying_evidence = _hash_payload(patched_ids)
    while True:
        _emit_progress(State.APPLYING, quiet=quiet)
        sm.transition(
            to_state=State.APPLYING,
            evidence_sha256=applying_evidence,
        )
        fix_result = _run_fix_core(
            root=root,
            findings=findings,
            apply_mode=True,
            suggest_mode=False,
            auto_llm=bool(args.auto_llm),
            force=False,
            max_llm_patches=None,
            recovery_branch_already_captured=recovery_branch_captured,
            quiet=quiet,
        )
        if args.auto_llm:
            recovery_branch_captured = True
        if fix_result.exit_code != EXIT_OK:
            sm.transition(
                to_state=State.FAILED,
                evidence_sha256=EVIDENCE_PLACEHOLDER,
                reason=f"fix_failed_exit_{fix_result.exit_code}",
            )
            return EXIT_FAILED
        applied_finding_ids |= fix_result.applied_finding_ids

        # --- VERIFYING -----------------------------------------------------
        _emit_progress(State.VERIFYING, quiet=quiet)
        sm.transition(
            to_state=State.VERIFYING,
            evidence_sha256=_hash_payload(sorted(applied_finding_ids)),
        )
        verify_result = _run_scan_core(
            root=root,
            full_sweep=True,
            analyzer_set=analyzer_set,
            scan_id=None,
            fresh_instance=False,
            quiet=quiet,
        )
        if verify_result.exit_code != EXIT_OK:
            sm.transition(
                to_state=State.FAILED,
                evidence_sha256=EVIDENCE_PLACEHOLDER,
                reason=f"verify_scan_failed_exit_{verify_result.exit_code}",
            )
            return EXIT_FAILED
        post_finding_ids = sorted(
            _finding_id(f) for f in verify_result.findings
        )

        # AC-3 from discovery: every applied id must be absent.
        post_set = set(post_finding_ids)
        unresolved = applied_finding_ids & post_set
        if not unresolved:
            sm.transition(
                to_state=State.DONE,
                evidence_sha256=_hash_payload(post_finding_ids),
            )
            # ARCH-015: post-DONE branch-and-commit policy. Fires only
            # when at least one finding was applied; never on FAILED /
            # HUMAN_REVIEW / RETRY exit paths (those return earlier).
            if applied_finding_ids:
                from autofix.cli.post_fix_policy import (
                    apply_post_fix_policy,
                )
                apply_post_fix_policy(
                    root=root,
                    run_id=sm.run_id,
                    applied_finding_ids=frozenset(applied_finding_ids),
                    policy=getattr(args, "post_fix", None),
                    quiet=quiet,
                )
            return EXIT_OK

        if attempt >= args.max_retries:
            sm.transition(
                to_state=State.FAILED,
                evidence_sha256=EVIDENCE_PLACEHOLDER,
                reason="max_retries_exhausted",
            )
            return EXIT_FAILED

        # --- RETRY ---------------------------------------------------------
        attempt += 1
        _emit_progress(State.RETRY, quiet=quiet)
        sm.transition(
            to_state=State.RETRY,
            evidence_sha256=_hash_payload(post_finding_ids),
            reason=f"attempt_{attempt}",
        )
        # Re-coordinate against fresh, deduped post-scan findings.
        findings = _dedup_findings(list(verify_result.findings))
        _emit_progress(State.TRIAGING, quiet=quiet)
        sm.transition(
            to_state=State.TRIAGING,
            evidence_sha256=_hash_payload(
                sorted(_finding_id(f) for f in findings)
            ),
        )
        # Re-coordinate + re-materialize patches for the new task list.
        retry_tasks = coordinate_repairs(
            findings, threshold=LLM_PATCH_THRESHOLD, root=None
        )
        retry_llm_tasks = [
            t for t in retry_tasks if t.tier == RepairTier.LLM_PATCH
        ]
        retry_task_ids = sorted(
            _finding_id(t.finding) for t in retry_llm_tasks
        )
        _emit_progress(State.PLANNING, quiet=quiet)
        sm.transition(
            to_state=State.PLANNING,
            evidence_sha256=_hash_payload(retry_task_ids),
        )
        retry_successful: list[str] = []
        for task in retry_llm_tasks:
            patch = produce_patch(task, root=root)
            if patch is not None:
                retry_successful.append(_finding_id(task.finding))
        applying_evidence = _hash_payload(sorted(retry_successful))


def run(args: argparse.Namespace) -> int:
    """Execute the ``autofix run`` subcommand; return a process exit code.

    Dispatches between the single-shot path (default) and the
    long-running watch path (``--watch``). All combinatorics
    validation happens here ONCE, before any session work — bad
    flag combinations exit ``EXIT_USAGE_ERROR`` regardless of
    ``--watch``.
    """
    # --- 0. Combinatorics validation (mirrors fix_command) ---------------
    if args.suggest and args.auto_llm:
        print(
            "autofix: --suggest and --auto-llm are mutually exclusive",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR
    if args.auto_llm and not args.apply:
        print("autofix: --auto-llm requires --apply", file=sys.stderr)
        return EXIT_USAGE_ERROR
    if args.max_retries < 0:
        print(
            f"autofix: --max-retries must be non-negative, got {args.max_retries}",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR

    analyzer_set = _resolve_analyzer_set(args)

    if not getattr(args, "watch", False):
        # Single-shot path: identical to ARCH-010 behavior.
        return _run_one_cycle(args, analyzer_set, fresh_instance=False)

    # --- Watch path -------------------------------------------------------
    from autofix.cli._watch_loop import run_watch_loop
    from autofix.cli.watch_command import (
        WatcherSession,
        _safety_sweep_seconds,
        _watch_once_enabled,
    )
    from autofix.events.schema import ChangeSet

    try:
        safety_sweep_seconds = _safety_sweep_seconds(
            getattr(args, "safety_sweep", None)
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE_ERROR

    try:
        session = WatcherSession(args.root, getattr(args, "since", None))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE_ERROR

    try:
        session.subscribe()
    except Exception as exc:
        print(
            f"pywatchman is required for `autofix watch`. ({exc})",
            file=sys.stderr,
        )
        session.close()
        return EXIT_USAGE_ERROR

    quiet: bool = bool(args.quiet)
    cycle_counter = {"n": 0}

    def _dispatcher(changeset: ChangeSet) -> None:
        cycle_counter["n"] += 1
        if not quiet:
            print(
                f"autofix: [CYCLE {cycle_counter['n']}] dispatching workflow...",
                file=sys.stderr,
                flush=True,
            )
        _run_one_cycle(
            args,
            analyzer_set,
            fresh_instance=changeset.is_fresh_instance,
        )

    return run_watch_loop(
        session,
        _dispatcher,
        safety_sweep_seconds=safety_sweep_seconds,
        once=_watch_once_enabled(),
    )


__all__ = ["HELP_DESCRIPTION", "HELP_EPILOG", "add_arguments", "run"]
