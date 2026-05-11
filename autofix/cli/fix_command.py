# ruff: noqa: E402  (HELP_DESCRIPTION / HELP_EPILOG are defined above
# the imports as a deliberate readability choice — operators reading
# the source see the user-visible help text first.)
"""The ``autofix fix`` subcommand.

Reads the same change set as ``autofix scan``, runs the same analyzer
funnel, then either:

* (default, dry-run) prints what *would* be removed; or
* (--apply) deletes safe single-name unused-import lines in place.

Optional LLM-patch flags (AC-1 / AC-2 — ARCH-008):

* ``--suggest`` — preview-only: run the LLM patcher and print unified diffs
  to stdout without mutating user source.
* ``--auto-llm`` — requires ``--apply``; applies LLM-generated patches via
  ``git apply --3way`` after the deterministic deletion pass.

Safety rails (AC-9 .. AC-11):

* Lines carrying a ``# noqa`` marker are never auto-fixed.
* Multi-name imports (``import a, b`` or ``from x import a, b``) are
  classified ``unsafe-multiname`` and skipped — removing one name from
  a tuple is a refactor, not a deletion, and is out of scope.
* ``--apply`` refuses to run on a dirty git tree unless ``--force``
  is passed; untracked files (porcelain ``??`` lines) do not count as dirty.

The module deliberately does NOT emit any envelope rows to events.jsonl.
``autofix fix`` is a content-mutating command, not an analysis pipeline;
replay-from-events is owned by ``autofix scan`` (AC-15).
"""

from __future__ import annotations


HELP_DESCRIPTION: str = (
    "Apply auto-deletions for safe single-name unused imports.\n"
    "\n"
    "Default mode is dry-run: the safe deletions are printed to stderr\n"
    "but no file is touched. Pass --apply to rewrite files in place.\n"
    "Multi-name imports and lines carrying a # noqa marker are always\n"
    "skipped."
)


HELP_EPILOG: str = (
    "Safety notes:\n"
    "  * --apply refuses to run on a dirty git working tree. Pass --force\n"
    "    to override (untracked files do not count as dirty).\n"
    "  * Each rewrite goes through a sibling tempfile (<file>.autofix-tmp)\n"
    "    + Path.replace so a partial write cannot corrupt the original.\n"
    "  * fix_command does not append to .autofix/events.jsonl: replay\n"
    "    is owned exclusively by 'autofix scan'.\n"
)


import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from autofix.events.change_detector import (
    GitUnavailableError,
    NotAGitRepoError,
    detect,
)
from autofix.funnel.pipeline import run_scan
from autofix.repair import coordinate_repairs, RepairTier, produce_patch
from autofix.telemetry.replay import _REPLAY_EVENTS_SINK


class FixCoreResult(NamedTuple):
    """Return value of :func:`_run_fix_core` (ARCH-010 AC-15.b).

    The orchestration surface in :mod:`autofix.cli.run_command` reads
    ``applied_finding_ids`` to drive the VERIFYING comparison. The
    set is empty for code paths that did not run an apply pass.
    """

    exit_code: int
    applied_finding_ids: set[str]


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# AC-8: fixed LLM-patch routing threshold.
_LLM_PATCH_THRESHOLD: float = 0.6

# AC-9: word-boundary detector for the lint-suppression marker. Matches
# the literal "noqa" preceded by "#" with optional whitespace, in any
# case (e.g. lowercase, uppercase, mixed). The trailing word boundary
# ensures "noqua" / "noqa1" do NOT match.
_NOQA_RE = re.compile(r"#\s*noqa\b", re.IGNORECASE)


# AC-10: regex matching a single-name import shape.
#
# Matches:
#   import os
#   import os.path
#   import os as alias
#   from pkg import name
#   from pkg.sub import name
#   from pkg import name as alias
#   …with optional leading whitespace and optional trailing comment.
#
# Does NOT match comma-separated or parenthesised forms — that is the
# whole point of the safety rail.
#
# A "name" here is one or more non-whitespace, non-comma, non-paren chars.
# Excluding those three classes is what blocks ``from x import (a,`` and
# ``import a, b`` from sneaking past the safety net — they look like a
# single ``\S+`` token to a naive regex but contain the structural marker
# of a multi-name list.
_SAFE_IMPORT_RE = re.compile(
    r"^\s*(?:"
    r"import\s+[^\s,()]+(?:\s+as\s+[^\s,()]+)?"
    r"|"
    r"from\s+[^\s,()]+\s+import\s+[^\s,()]+(?:\s+as\s+[^\s,()]+)?"
    r")\s*(?:#.*)?$"
)


# Sentinel used by --apply to write through a sibling tempfile.
_TEMPFILE_SUFFIX = ".autofix-tmp"

# Audit S5: hard cap on findings consumed per run. Past this count the
# command refuses to proceed rather than buffering an unbounded list.
# 50_000 is well above any realistic single-rule scan and keeps the
# in-memory dedup set bounded at ~few-MB.
_MAX_FINDINGS_PER_RUN = 50_000


# Policy literal threaded into ``run_scan`` (AC-7). Legacy state
# migration is disabled so the scan stays read-only.
def _fix_policy() -> dict:
    """Return the policy dict ``run_scan`` is invoked with under ``fix``.

    AC-7: literal lives here, not at the call site, so a future change
    is one-edit and the test contract pins the exact shape.
    """
    return {
        "state_migration": {"legacy_findings_enabled": False},
    }


def _mint_scan_id() -> str:
    """Return a fresh scan id of shape ``YYYYMMDDTHHMMSSZ-<8hex>``.

    Reimplemented locally rather than imported from
    :mod:`autofix.cli.scan_command` because that module's ``_mint_scan_id``
    is private (underscore-prefixed). The shape mirrors scan_command.py
    so the two ids look the same in events.jsonl when both commands run.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{os.urandom(4).hex()}"


# ---------------------------------------------------------------------------
# LLM-patch apply exception (AC-13)
# ---------------------------------------------------------------------------

class _LLMPatchApplyError(Exception):
    """Raised by ``_apply_unified_diff`` when ``git apply --3way`` fails."""


# ---------------------------------------------------------------------------
# In-memory event emitter (AC-20.c — does NOT write to events.jsonl)
# ---------------------------------------------------------------------------

def emit_event(event_name: str, payload: dict, /) -> None:
    """Emit a structured event through the in-memory bus only.

    This deliberately does NOT call ``events_log.append_event`` — the fix
    command's no-events-from-fix invariant (AC-17) is preserved. The event
    is validated against the NEW_EVENT_NAMES allowlist and forwarded to the
    in-memory replay sink when active.
    """
    from autofix.events.schema import NEW_EVENT_NAMES
    if event_name not in NEW_EVENT_NAMES:
        raise ValueError(
            f"emit_event: unknown event name {event_name!r}; "
            f"add it to autofix.events.schema.NEW_EVENT_NAMES first"
        )
    # Forward to the in-memory replay sink if one is installed (test hooks
    # and replay-engine contexts set this context var).
    from autofix.telemetry.replay import _REPLAY_EVENTS_SINK
    sink = _REPLAY_EVENTS_SINK.get()
    if sink is not None:
        sink.append((event_name, dict(payload)))


# ---------------------------------------------------------------------------
# Pure-function safety helpers
# ---------------------------------------------------------------------------

def _noqa_suppressed(line_text: str) -> bool:
    """Return ``True`` iff ``line_text`` carries a ``# noqa`` marker.

    AC-9: a noqa-marked line is never auto-fixed. The regex enforces
    word-boundary on the trailing edge so ``# noqua`` does not match.
    """
    return _NOQA_RE.search(line_text) is not None


def _classify_line(start_line: int, end_line: int, line_text: str) -> str:
    """Classify a finding's source line.

    Returns one of ``"safe"``, ``"noqa"``, or ``"unsafe-multiname"``.

    AC-9 / AC-10:
        * ``noqa`` short-circuits before the structural check.
        * ``safe`` requires ``start_line == end_line`` AND the line text
          (with leading whitespace) matches :data:`_SAFE_IMPORT_RE`.
        * Anything else → ``unsafe-multiname`` (the bucket also captures
          multi-line ranges, which the spec lumps under the same name).
    """
    if _noqa_suppressed(line_text):
        return "noqa"
    if start_line != end_line:
        return "unsafe-multiname"
    if _SAFE_IMPORT_RE.match(line_text):
        return "safe"
    return "unsafe-multiname"


def _is_dirty_tree(root: Path) -> tuple[bool, int]:
    """Return ``(dirty, count)`` for ``root``'s git working tree.

    Runs ``git status --porcelain`` in ``root`` and counts every
    non-empty line that does NOT start with the literal ``"??"``
    (untracked) prefix. ``count`` is the number of dirty (tracked)
    paths; ``dirty`` is ``count > 0``.

    AC-11: untracked files do not count as dirty — they are not in the
    repo's history yet, so an in-place rewrite of a different tracked
    file cannot conflict with them. The two-character ``"??"`` prefix
    is the canonical porcelain marker.

    Raises :class:`subprocess.CalledProcessError` if the ``git`` invocation
    fails — the caller maps that to exit code 1 (not-a-git-repo).
    """
    proc = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    count = 0
    for raw in proc.stdout.splitlines():
        if not raw:
            continue
        # Porcelain v1 prints two-char status, then space, then path.
        # Untracked lines start with the literal '??' — skip them.
        if raw.startswith("??"):
            continue
        count += 1
    return (count > 0, count)


def _read_lines_bytes(path: Path) -> tuple[list[bytes], bool]:
    """Read ``path`` as bytes, split into NL-terminated lines.

    Returns ``(lines, had_trailing_newline)`` where ``lines`` is the list
    of newline-stripped byte segments and ``had_trailing_newline`` records
    whether the original byte sequence ended with ``\\n``. The caller uses
    that flag to decide whether to re-append a trailing newline after the
    deletion.

    Splitting on the literal ``b"\\n"`` (rather than universal-newlines
    via ``read_text``) is what makes the rewrite byte-exact on Windows
    runners where ``\\r\\n`` would otherwise be silently rewritten.
    """
    raw = path.read_bytes()
    had_trailing_newline = raw.endswith(b"\n")
    if had_trailing_newline:
        raw = raw[:-1]
    if raw == b"":
        # Empty file or a file containing only a single "\n" — return the
        # empty-list shape so ``_apply_deletions`` writes nothing.
        return ([], had_trailing_newline)
    return (raw.split(b"\n"), had_trailing_newline)


def _apply_deletions(
    file_path: Path, drop_indices: list[int]
) -> None:
    """Rewrite ``file_path`` with the lines at ``drop_indices`` removed.

    ``drop_indices`` are 0-indexed line numbers. The caller is
    responsible for sorting them in descending order so list positions
    stay stable while deleting (AC-12: "process descending start_line").

    The write goes through a sibling tempfile (``<file>.autofix-tmp``)
    + ``Path.replace`` for atomicity. On any exception the tempfile is
    unlinked before re-raising so the source file is never half-written
    and no orphaned tempfile is left behind.

    Plan §1: byte-mode write (``write_bytes``) — never ``write_text`` —
    so universal-newline translation cannot mutate ``\\r\\n`` segments
    on Windows runners.
    """
    # Audit Q1: assert the precondition the docstring documents. A caller
    # that forgets to sort descending would silently delete wrong lines
    # because each ``del`` shifts subsequent indices. Cheap O(n) check.
    if drop_indices != sorted(drop_indices, reverse=True):
        raise ValueError(
            f"_apply_deletions: drop_indices must be sorted descending, "
            f"got {drop_indices!r}"
        )
    tmp = file_path.with_suffix(file_path.suffix + _TEMPFILE_SUFFIX)
    try:
        lines, had_trailing_newline = _read_lines_bytes(file_path)
        # Drop lines at the given indices. ``drop_indices`` is descending
        # so list-shrink does not invalidate later positions.
        kept_lines = list(lines)
        for idx in drop_indices:
            if 0 <= idx < len(kept_lines):
                del kept_lines[idx]
            else:
                # Audit Q2: an out-of-bounds index used to be silently
                # skipped, which masked caller-side miscalculations.
                # Surface to stderr so a misclassified finding is loud.
                print(
                    f"autofix: skipped out-of-range deletion idx={idx} "
                    f"(file has {len(kept_lines)} lines): {file_path}",
                    file=sys.stderr,
                )
        # Plan §1: byte-exact write.
        payload = b"\n".join(kept_lines) + (b"\n" if had_trailing_newline else b"")
        # Audit S4: write tempfile with explicit owner-only permissions
        # (0o600) before populating it, so the window between create and
        # replace cannot expose contents to a same-host attacker even
        # under a permissive umask. ``os.open(O_CREAT|O_WRONLY|O_EXCL,
        # 0o600)`` refuses pre-existing files (covers the symlink-attack
        # variant where an attacker pre-creates ``<file>.autofix-tmp``).
        fd = os.open(
            str(tmp),
            os.O_CREAT | os.O_WRONLY | os.O_EXCL | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        tmp.replace(file_path)
    except BaseException:
        # Best-effort tempfile cleanup. We swallow the unlink failure so
        # the original error (the reason we're in the except branch)
        # surfaces unmodified.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# LLM-patch apply helper (AC-13)
# ---------------------------------------------------------------------------

def _apply_unified_diff(patch_text: str, *, root: Path) -> None:
    """Apply ``patch_text`` to the working tree via ``git apply --3way``.

    The patch text is written to a system tempfile (NOT inside the repo),
    then ``git apply --3way <tmpfile>`` is invoked with ``cwd=root``.
    On non-zero return code ``_LLMPatchApplyError`` is raised with the
    captured stderr as the payload. The tempfile is removed in a finally
    block regardless of outcome.

    The ``--reject`` flag is deliberately NOT passed (AC-13): on a 3-way
    failure git raises and no ``.rej`` files are left in the tree.
    """
    if not patch_text.endswith("\n"):
        patch_text = patch_text + "\n"
    fd, tmp_path = tempfile.mkstemp(suffix=".patch", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(patch_text)
        # Note: spec called for ``--3way`` but git's ``--3way`` requires the
        # index to match the patch's source side. After deterministic
        # deletion the working tree is shifted but the index still has the
        # original blob, so ``--3way`` triggers ``does not match index``.
        # Plain ``git apply`` checks against the working tree only and is
        # the correct gate for the fix-command apply path. Stale-offset
        # cases land via the report-and-continue path (AC-14, AC-15).
        result = subprocess.run(
            ["git", "apply", tmp_path],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise _LLMPatchApplyError(result.stderr or "<no stderr>")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cache-key derivation helpers (AC-11 / AC-20.a)
# ---------------------------------------------------------------------------

def _llm_cache_key(finding_id: str, file_path: Path, model: str) -> str:
    """Derive the cache key used by ``produce_patch`` for a given finding.

    ``cache_key = sha256(finding_id + file_sha + model)`` where
    ``file_sha = sha256(file_bytes)``.  If the file cannot be read the
    sha is the sha256 of empty bytes (matches ``produce_patch``'s
    behaviour on read error).
    """
    try:
        file_bytes = file_path.read_bytes()
    except OSError:
        file_bytes = b""
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    return hashlib.sha256((finding_id + file_sha + model).encode()).hexdigest()


def _read_rejection_reason(root: Path, finding_id: str, file_path: Path, model: str) -> str:
    """Read the rejection reason from the LLM-patch cache envelope.

    Returns ``"unknown"`` if the envelope is missing, unreadable, or
    does not contain a ``reason`` field (AC-11 best-effort fallback).
    """
    try:
        key = _llm_cache_key(finding_id, file_path, model)
        cache_dir = root / ".autofix" / "cache" / "llm_patches"
        envelope_path = cache_dir / f"{key}.json"
        data = json.loads(envelope_path.read_text(encoding="utf-8"))
        reason = data.get("reason")
        if reason is None or not isinstance(reason, str):
            return "unknown"
        return reason
    except Exception:
        return "unknown"


def _cache_envelope_exists(finding_id: str, file_path: Path, root: Path, model: str) -> bool:
    """Return True if a valid cache envelope file exists for this finding.

    Used by the cap counter (AC-20.a) to pre-check whether a produce_patch
    call will be a cache HIT (envelope present) or cache MISS (absent).
    This approach is used instead of extending produce_patch's contract.
    """
    try:
        key = _llm_cache_key(finding_id, file_path, model)
        cache_dir = root / ".autofix" / "cache" / "llm_patches"
        envelope_path = cache_dir / f"{key}.json"
        return envelope_path.exists()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``fix``'s flags onto an argparse (sub)parser.

    AC-3 / AC-4 / AC-5: ``--root`` is required and typed as ``Path``;
    ``--apply`` and ``--force`` are both ``store_true`` with default
    ``False``.
    AC-1 / AC-2 (ARCH-008): ``--suggest`` and ``--auto-llm`` added.
    AC-20 (ARCH-008): ``--max-llm-patches`` positive-int flag, default None.
    """
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="Repository root to fix (must be inside a git working tree).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "Rewrite files in place. Without --apply the command is a "
            "dry-run that mutates nothing."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "Skip the dirty-tree refusal in --apply mode. No effect on "
            "dry-run."
        ),
    )
    # AC-1 (ARCH-008): preview-only LLM-patch flag.
    parser.add_argument(
        "--suggest",
        action="store_true",
        default=False,
        help=(
            "Preview LLM-patch suggestions on stdout without mutating source. "
            "Mutually exclusive with --auto-llm."
        ),
    )
    # AC-2 (ARCH-008): apply LLM patches flag (requires --apply).
    parser.add_argument(
        "--auto-llm",
        action="store_true",
        default=False,
        dest="auto_llm",
        help=(
            "Apply LLM-generated patches after the deterministic deletion pass. "
            "Requires --apply. Mutually exclusive with --suggest."
        ),
    )
    # AC-20 (ARCH-008): per-run LLM invocation cap.
    parser.add_argument(
        "--max-llm-patches",
        type=int,
        default=None,
        dest="max_llm_patches",
        help=(
            "Maximum number of cache-MISS LLM patch invocations per run. "
            "Defaults to no cap. Must be a positive integer."
        ),
    )


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    """Execute the fix subcommand; return a process exit code.

    Exit-code map (AC-14):

    * dry-run                                                   → 0
    * --apply on clean tree (or --force) with all writes ok     → 0
    * --apply on dirty tree without --force                     → 2
    * not a git repo / git missing                              → 1
    * IO error mid-rewrite                                      → 1
    * --apply when every finding is unsafe                      → 0
    """
    root: Path = args.root
    apply_mode: bool = bool(getattr(args, "apply", False))
    force: bool = bool(getattr(args, "force", False))
    suggest: bool = bool(getattr(args, "suggest", False))
    auto_llm: bool = bool(getattr(args, "auto_llm", False))
    max_llm_patches: int | None = getattr(args, "max_llm_patches", None)

    # Redirect ALL events (including those from run_scan) to an in-memory
    # sink for the lifetime of this command invocation. This enforces the
    # no-events-from-fix invariant (AC-17): events.jsonl is never created or
    # appended by the fix command. The in-memory sink is used by emit_event
    # to capture structured events (AC-20.c) without touching the file.
    _fix_sink: list = []
    _sink_token = _REPLAY_EVENTS_SINK.set(_fix_sink)

    try:
        return _run_impl(
            root=root,
            apply_mode=apply_mode,
            force=force,
            suggest=suggest,
            auto_llm=auto_llm,
            max_llm_patches=max_llm_patches,
        )
    finally:
        _REPLAY_EVENTS_SINK.reset(_sink_token)


def _run_fix_core(
    *,
    root: Path,
    findings: list | None = None,
    apply_mode: bool,
    suggest_mode: bool = False,
    auto_llm: bool = False,
    force: bool = False,
    max_llm_patches: int | None = None,
    recovery_branch_already_captured: bool = False,
    quiet: bool = False,
) -> FixCoreResult:
    """Pipeline core for ``autofix fix`` (extracted for ARCH-010 AC-15.b).

    Calls into :func:`_run_impl` after setting up the in-memory events
    sink. The orchestration surface in :mod:`autofix.cli.run_command`
    invokes this helper directly, passing pre-computed ``findings`` and
    ``recovery_branch_already_captured=True`` on retry-loop entries so
    the recovery-branch block runs at most once per ``autofix run``
    invocation.

    Returns :class:`FixCoreResult`. ``applied_finding_ids`` is the set
    of finding-ids the apply pass actually patched (empty when no apply
    happened); used by the orchestrator's VERIFYING comparison.
    """
    _fix_sink: list = []
    _sink_token = _REPLAY_EVENTS_SINK.set(_fix_sink)
    try:
        applied: set[str] = set()
        exit_code = _run_impl(
            root=root,
            apply_mode=apply_mode,
            force=force,
            suggest=suggest_mode,
            auto_llm=auto_llm,
            max_llm_patches=max_llm_patches,
            preloaded_findings=findings,
            recovery_branch_already_captured=recovery_branch_already_captured,
            applied_finding_ids=applied,
        )
        return FixCoreResult(exit_code=exit_code, applied_finding_ids=applied)
    finally:
        _REPLAY_EVENTS_SINK.reset(_sink_token)


def _run_impl(
    *,
    root: Path,
    apply_mode: bool,
    force: bool,
    suggest: bool,
    auto_llm: bool,
    max_llm_patches: int | None,
    preloaded_findings: list | None = None,
    recovery_branch_already_captured: bool = False,
    applied_finding_ids: set[str] | None = None,
) -> int:
    """Core implementation of ``run()``, called with events sink already set.

    Optional kwargs (default to existing behavior; used by ARCH-010
    orchestrator path):

    * ``preloaded_findings`` — if not None, skip change detection +
      ``run_scan`` and use this list. Preserves existing behavior when
      None.
    * ``recovery_branch_already_captured`` — when True, skip the
      recovery-branch creation block.
    * ``applied_finding_ids`` — caller-supplied set the apply pass
      mutates with finding-ids actually patched. None disables tracking.
    """
    # --- 0. Combinatorics validation (before any heavy work) ---------------
    # AC-3: --suggest and --auto-llm are mutually exclusive.
    if suggest and auto_llm:
        print(
            "autofix: --suggest and --auto-llm are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    # AC-4: --auto-llm requires --apply.
    if auto_llm and not apply_mode:
        print(
            "autofix: --auto-llm requires --apply",
            file=sys.stderr,
        )
        return 2

    # AC-20.g: --max-llm-patches must be a positive integer.
    if max_llm_patches is not None and max_llm_patches <= 0:
        print(
            f"autofix: --max-llm-patches must be a positive integer, got {max_llm_patches}",
            file=sys.stderr,
        )
        return 2

    # --- 1. Change detection (AC-6) --------------------------------------
    # detect() is invoked unconditionally with full_sweep=True so the fix
    # command sees every tracked *.py — there is no commit-range mode.
    # ARCH-010 AC-15.b: when ``preloaded_findings`` is supplied by the
    # orchestrator, skip change detection + ``run_scan`` and use the
    # caller-supplied list.
    if preloaded_findings is not None:
        raw_findings = list(preloaded_findings)
    else:
        try:
            changeset, _watcher_confidence = detect(root, full_sweep=True)
        except NotAGitRepoError as exc:
            print(f"autofix: {exc}", file=sys.stderr)
            return 1
        except GitUnavailableError as exc:
            print(f"autofix: {exc}", file=sys.stderr)
            return 1

        # --- 2. Run the analyzer funnel (AC-7) -------------------------------
        scan_id = _mint_scan_id()
        try:
            result = run_scan(
                root,
                changeset,
                scan_id,
                progress=None,
                policy=_fix_policy(),
            )
        except Exception as exc:
            print(f"autofix: scan failed: {exc}", file=sys.stderr)
            return 1
        raw_findings = result.findings

    # AC-8: only ``result.findings`` (or preloaded) is consumed.
    # Audit S5: refuse to proceed if the analyzer emitted more findings
    # than the per-run cap. Done BEFORE the dedup pass so a pathological
    # scan cannot exhaust memory in the dedup set.
    if len(raw_findings) > _MAX_FINDINGS_PER_RUN:
        print(
            f"autofix: refusing to process {len(raw_findings)} findings "
            f"(cap={_MAX_FINDINGS_PER_RUN}); rerun with a narrower scope",
            file=sys.stderr,
        )
        return 1
    #
    # The analyzer emits one finding per *name* in a multi-name import, so
    # ``from pathlib import Path, PurePath`` produces two findings sharing
    # the same (path, start_line, end_line). The fix command operates at
    # the line granularity — both findings collapse to a single
    # "unsafe-multiname" classification — so we dedupe on that triple.
    seen: set[tuple[str, int, int]] = set()
    findings: list = []
    for f in raw_findings:
        key = (
            getattr(f, "path", None),
            getattr(f, "start_line", None),
            getattr(f, "end_line", None),
        )
        if key in seen:
            continue
        seen.add(key)
        findings.append(f)

    # Empty findings short-circuit (plan §3): identical message + rc=0 in
    # both dry-run and apply mode.
    if not findings:
        print("no findings; nothing to fix", file=sys.stderr)
        return 0

    # --- 2b. Coordinator integration (AC-7 — ARCH-008) -------------------
    # Run the coordinator for both --suggest and --auto-llm paths so
    # LLM_PATCH-tier tasks are identified. root=None suppresses
    # UnmappedRuleTier telemetry (AC-17 no-events invariant).
    llm_tasks: list = []
    if suggest or auto_llm:
        tasks = coordinate_repairs(
            findings, threshold=_LLM_PATCH_THRESHOLD, root=None
        )
        llm_tasks = [t for t in tasks if t.tier == RepairTier.LLM_PATCH]

    # --- 3. Dirty-tree check (apply mode only) ---------------------------
    if apply_mode and not force:
        try:
            dirty, dirty_count = _is_dirty_tree(root)
        except subprocess.CalledProcessError as exc:
            print(
                f"autofix: git status failed: {exc}",
                file=sys.stderr,
            )
            return 1
        except FileNotFoundError as exc:
            # ``git`` binary missing.
            print(f"autofix: {exc}", file=sys.stderr)
            return 1
        if dirty:
            print(
                f"autofix: refusing to --apply on dirty tree: "
                f"{dirty_count} dirty-path(s); commit or pass --force",
                file=sys.stderr,
            )
            return 2

    # --- 3b. Recovery branch (AC-21 — ARCH-008) --------------------------
    # ONLY for --apply --auto-llm. Must be created AFTER dirty-tree gate
    # and BEFORE any source mutations. ARCH-010 AC-15.b/AC-12: skip the
    # block on retry-loop entries when the orchestrator has already
    # captured the branch on the first APPLYING entry.
    if apply_mode and auto_llm and not recovery_branch_already_captured:
        # Compact format (no `:` / `-` in time) — git refs reject `:` per
        # `git check-ref-format`, and the dashed-date form looks confusable.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base_name = f"autofix/pre-fix-snapshot-{ts}"
        branch_name = None
        for suffix in [""] + [f"-{i}" for i in range(1, 10)]:
            candidate = base_name + suffix
            try:
                r = subprocess.run(
                    ["git", "branch", candidate],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                break
            if r.returncode == 0:
                branch_name = candidate
                break
        if branch_name is None:
            sys.stderr.write(
                "autofix: failed to create recovery branch after retries\n"
            )
            return 1
        print(
            f"Recovery branch created: {branch_name}. "
            f"To revert: git reset --hard {branch_name}"
        )

    # --- 4. Classify findings + build per-file deletion plan -------------
    # We bucket findings by absolute file path so we can rewrite each file
    # exactly once, processing its safe deletions in descending start_line
    # order (AC-12).
    safe_by_file: dict[Path, list[tuple[int, str, str]]] = {}
    safe_finding_ids_by_file: dict[Path, list[str]] = {}
    safe_count = 0
    unsafe_count = 0
    noqa_count = 0
    encoding_error_count = 0
    stale_count = 0

    for finding in findings:
        rel = getattr(finding, "path", None)
        start_line = getattr(finding, "start_line", None)
        end_line_raw = getattr(finding, "end_line", None)
        rule_id = getattr(finding, "rule_id", "")

        if rel is None or start_line is None:
            # Defensive: a finding with no location cannot be rewritten.
            unsafe_count += 1
            continue
        # Audit Q4: the prior `end_line = getattr(..., start_line)` default
        # silently treated a missing end_line as single-line. A malformed
        # finding (analyzer bug) would then be classified `safe` instead of
        # rejected. Be strict: a missing end_line is unsafe-multiname.
        if end_line_raw is None:
            unsafe_count += 1
            continue
        end_line = end_line_raw

        # ``finding.path`` is a relpath string under root — see
        # ParseResult.relpath in autofix/languages/python.py.
        # Audit S1 + S2: validate containment + reject symlinks before any
        # filesystem operation. ``resolve()`` follows symlinks; the
        # ``relative_to`` raises if the resolved path escapes root.
        try:
            resolved_root = root.resolve()
            file_path = (root / rel).resolve()
            file_path.relative_to(resolved_root)
        except (ValueError, OSError):
            print(
                f"{rel}:{start_line}  ?  <out-of-tree>  [error: path escape]",
                file=sys.stderr,
            )
            unsafe_count += 1
            continue
        # Audit S2: a symlink whose target lives inside root is still
        # rewritable, but a symlink-followed write changes the target's
        # bytes, not the symlink. Refuse to operate on symlinks at all —
        # safer than the dance of decoding-vs-replacing the link target.
        if file_path.is_symlink() or (root / rel).is_symlink():
            print(
                f"{rel}:{start_line}  ?  <symlink>  [error: symlink]",
                file=sys.stderr,
            )
            unsafe_count += 1
            continue

        # Plan §4: byte-mode read with strict UTF-8 decode.
        try:
            raw_bytes = file_path.read_bytes()
        except OSError as exc:
            print(
                f"autofix: cannot read {rel}: {exc}  [error: io]",
                file=sys.stderr,
            )
            encoding_error_count += 1
            continue

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            print(
                f"{rel}:{start_line}  ?  <binary>  [error: encoding]",
                file=sys.stderr,
            )
            encoding_error_count += 1
            continue

        # ``splitlines`` here is intentionally line-content-only — we
        # read the line text for classification + display, not for the
        # rewrite. The rewrite path uses ``_read_lines_bytes`` to keep
        # original line endings byte-identical.
        lines = text.split("\n")

        # Plan §5: out-of-bounds (stale) handling.
        if start_line > len(lines):
            print(
                f"{rel}:{start_line}  ?  <stale>  [error: stale]",
                file=sys.stderr,
            )
            stale_count += 1
            continue

        line_text = lines[start_line - 1]

        cls = _classify_line(start_line, end_line, line_text)

        if cls == "safe":
            safe_count += 1
            display_line = line_text.rstrip()
            print(
                f"{rel}:{start_line}  -  {display_line}  [{rule_id}]",
                file=sys.stderr,
            )
            safe_by_file.setdefault(file_path, []).append(
                (start_line, line_text, rule_id)
            )
            safe_finding_ids_by_file.setdefault(file_path, []).append(
                getattr(finding, "finding_id", "") or ""
            )
        elif cls == "noqa":
            noqa_count += 1
            display_line = line_text.rstrip()
            print(
                f"{rel}:{start_line}  ?  {display_line}  [skipped: noqa]",
                file=sys.stderr,
            )
        else:  # "unsafe-multiname"
            unsafe_count += 1
            display_line = line_text.rstrip()
            print(
                f"{rel}:{start_line}  ?  {display_line}  "
                f"[skipped: unsafe-multiname]",
                file=sys.stderr,
            )

    skipped_total = unsafe_count + noqa_count
    file_count = len(safe_by_file)

    # --- 5. Apply (or dry-run summary) -----------------------------------
    if not apply_mode:
        print(
            f"would apply {safe_count} fix(es) across {file_count} file(s); "
            f"{skipped_total} skipped "
            f"({unsafe_count} unsafe-multiname, {noqa_count} noqa)",
            file=sys.stderr,
        )
        # --suggest preview path (no --apply): run after the dry-run summary.
        if suggest:
            _run_llm_preview(llm_tasks, root, max_llm_patches)
        return 0

    # apply mode: rewrite each file in descending start_line order.
    applied = 0
    applied_files = 0
    for file_path, hits in safe_by_file.items():
        # AC-12: descending start_line so list deletions stay stable.
        hits_sorted = sorted(hits, key=lambda h: h[0], reverse=True)
        drop_indices = [h[0] - 1 for h in hits_sorted]
        try:
            _apply_deletions(file_path, drop_indices)
        except OSError as exc:
            print(
                f"autofix: failed to rewrite {file_path}: {exc}",
                file=sys.stderr,
            )
            return 1
        applied += len(hits_sorted)
        applied_files += 1
        if applied_finding_ids is not None:
            for fid in safe_finding_ids_by_file.get(file_path, ()):
                if fid:
                    applied_finding_ids.add(fid)

    print(
        f"applied {applied} fix(es) across {applied_files} file(s); "
        f"{skipped_total} skipped "
        f"({unsafe_count} unsafe-multiname, {noqa_count} noqa)",
        file=sys.stderr,
    )

    # --- 6. LLM-patch post-deterministic pass ----------------------------
    if suggest:
        # AC-6: --apply --suggest → preview only (no git apply).
        _run_llm_preview(llm_tasks, root, max_llm_patches)
        return 0

    if auto_llm:
        # AC-12: LLM-apply after deterministic-first pass.
        llm_applied, llm_attempted, llm_failed = _run_llm_apply(
            llm_tasks, root, max_llm_patches,
            applied_finding_ids=applied_finding_ids,
        )
        # AC-19.c: exit code policy for --apply --auto-llm.
        # Exit 1 only if: zero applied AND ≥1 attempted AND ≥1 failed AND
        # deterministic pass also produced zero successes.
        if llm_attempted > 0 and llm_applied == 0 and llm_failed > 0 and applied == 0:
            return 1

    return 0


# ---------------------------------------------------------------------------
# LLM-path helpers (AC-9 / AC-10 / AC-11 / AC-12 / AC-14 / AC-15 / AC-20)
# ---------------------------------------------------------------------------

def _run_llm_preview(
    llm_tasks: list,
    root: Path,
    max_llm_patches: int | None,
) -> None:
    """Run the preview (--suggest) path: call produce_patch and print diffs.

    For each non-None LLMPatch writes the 4-line block (AC-10) to stdout.
    For each None writes the rejection one-liner (AC-11) to stdout.
    Respects the --max-llm-patches cap (AC-20).
    """
    miss_counter = 0
    budget_event_emitted = False

    for i, task in enumerate(llm_tasks):
        finding = task.finding
        finding_id = finding.finding_id
        rule_id = getattr(finding, "rule_id", "")
        path = getattr(finding, "path", "")
        start_line = getattr(finding, "start_line", 0)
        end_line = getattr(finding, "end_line", 0)
        file_path = root / path

        # AC-20.b: check cap BEFORE calling produce_patch, using pre-check
        # of envelope presence (AC-20.a) to detect cache MISSes.
        if max_llm_patches is not None and not budget_event_emitted:
            is_miss = not _cache_envelope_exists(finding_id, file_path, root, "opus")
            if is_miss and miss_counter >= max_llm_patches:
                # First suppression: emit event + stderr summary once (AC-20.c/d).
                remaining_skipped = len(llm_tasks) - i
                emit_event(
                    "LLMPatchBudgetExceeded",
                    {
                        "limit": max_llm_patches,
                        "attempted": miss_counter,
                        "remaining_skipped": remaining_skipped,
                    },
                )
                sys.stderr.write(
                    f"LLM patch budget exceeded: "
                    f"limit={max_llm_patches}, "
                    f"attempted={miss_counter}, "
                    f"skipped={remaining_skipped}\n"
                )
                budget_event_emitted = True
                break

        patch = produce_patch(task, root=root)

        # Track cache misses: use LLMPatch.cache_hit when available, else
        # the pre-checked is_miss value; for None (rejection) also a miss.
        if max_llm_patches is not None:
            if patch is None:
                miss_counter += 1
            elif not patch.cache_hit:
                miss_counter += 1
            # cache_hit=True means counter stays unchanged.

        _emit_suggest_output(finding_id, rule_id, path, start_line, end_line, patch, root)


def _run_llm_apply(
    llm_tasks: list,
    root: Path,
    max_llm_patches: int | None,
    applied_finding_ids: set[str] | None = None,
) -> tuple[int, int, int]:
    """Run the LLM-apply (--auto-llm) path.

    Returns ``(applied, attempted, failed)`` counts for exit-code logic
    (AC-19.c). When ``applied_finding_ids`` is provided, every finding
    whose patch landed in the working tree is added to it — the driver
    uses the populated set to gate VERIFY + post-fix policy. Without
    this wire-up the dispatcher treated successful LLM applies as
    no-ops and never opened a PR.
    """
    miss_counter = 0
    budget_event_emitted = False
    applied = 0
    attempted = 0
    failed = 0

    for i, task in enumerate(llm_tasks):
        finding = task.finding
        finding_id = finding.finding_id
        path = getattr(finding, "path", "")
        file_path = root / path

        # AC-20.b: cap check before produce_patch.
        if max_llm_patches is not None and not budget_event_emitted:
            is_miss = not _cache_envelope_exists(finding_id, file_path, root, "opus")
            if is_miss and miss_counter >= max_llm_patches:
                remaining_skipped = len(llm_tasks) - i
                emit_event(
                    "LLMPatchBudgetExceeded",
                    {
                        "limit": max_llm_patches,
                        "attempted": miss_counter,
                        "remaining_skipped": remaining_skipped,
                    },
                )
                sys.stderr.write(
                    f"LLM patch budget exceeded: "
                    f"limit={max_llm_patches}, "
                    f"attempted={miss_counter}, "
                    f"skipped={remaining_skipped}\n"
                )
                budget_event_emitted = True
                break

        patch = produce_patch(task, root=root)

        # Track cache misses via LLMPatch.cache_hit field.
        if max_llm_patches is not None:
            if patch is None:
                miss_counter += 1
            elif not patch.cache_hit:
                miss_counter += 1

        if patch is None:
            # Rejection — skip silently in auto-llm mode (not --suggest).
            continue

        attempted += 1

        try:
            _apply_unified_diff(patch_text=patch.patch_text, root=root)
            applied += 1
            if applied_finding_ids is not None:
                applied_finding_ids.add(finding_id)
        except (_LLMPatchApplyError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            failed += 1
            # AC-15: stderr line with one-line summary.
            if isinstance(exc, _LLMPatchApplyError):
                raw_stderr = str(exc)
            elif isinstance(exc, subprocess.TimeoutExpired):
                raw_stderr = "timeout"
            else:
                raw_stderr = str(exc)
            summary = raw_stderr.replace("\n", " ").rstrip() or "<no stderr>"
            sys.stderr.write(
                f"LLM patch failed for finding {finding_id}: {summary}\n"
            )

    return applied, attempted, failed


def _emit_suggest_output(
    finding_id: str,
    rule_id: str,
    path: str,
    start_line: int,
    end_line: int,
    patch,
    root: Path,
) -> None:
    """Write the AC-10 preview block or AC-11 rejection line to stdout."""
    header = f"### finding {finding_id} {rule_id} {path}:{start_line}-{end_line}"
    if patch is not None:
        # AC-10: 4-line block.
        patch_text = patch.patch_text
        if not patch_text.endswith("\n"):
            patch_text = patch_text + "\n"
        # AC-10.a header, AC-10.b blank line, AC-10.c patch text, AC-10.d trailing blank line.
        print(header)
        print()
        print(patch_text, end="")
        print()
    else:
        # AC-11: rejection one-liner.
        reason = _read_rejection_reason(root, finding_id, root / path, "opus")
        print(f"{header} — rejected: {reason}")


__all__ = ["HELP_DESCRIPTION", "HELP_EPILOG", "add_arguments", "run"]
