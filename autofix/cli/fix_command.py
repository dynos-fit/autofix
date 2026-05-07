"""The ``autofix fix`` subcommand.

Reads the same change set as ``autofix scan``, runs the same analyzer
funnel, then either:

* (default, dry-run) prints what *would* be removed; or
* (--apply) deletes safe single-name unused-import lines in place.

Safety rails (AC-9 .. AC-11):

* Lines carrying a ``# noqa`` marker are never auto-fixed.
* Multi-name imports (``import a, b`` or ``from x import a, b``) are
  classified ``unsafe-multiname`` and skipped — removing one name from
  a tuple is a refactor, not a deletion, and is out of scope.
* ``--apply`` refuses to run on a dirty git tree unless ``--force`` is
  passed; untracked files (porcelain ``??`` lines) do not count as dirty.

The module deliberately does NOT emit any envelope rows. ``autofix fix``
is a content-mutating command, not an analysis pipeline; replay-from-
events is owned by ``autofix scan`` (AC-15).
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
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from autofix.events.change_detector import (
    GitUnavailableError,
    NotAGitRepoError,
    detect,
)
from autofix.funnel.pipeline import run_scan


# ---------------------------------------------------------------------------
# Private constants
# ---------------------------------------------------------------------------

# AC-9: word-boundary noqa detector. Matches '# noqa', '#noqa',
# '# NOQA: F401', '# noqa:F401' — any case, any spacing between '#' and
# 'noqa', word-boundary on the trailing edge so 'noqua' does NOT match.
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


# Policy literal threaded into ``run_scan`` (AC-7). Embedding sidecar is
# disabled so a fix run never produces a SARIF or sidecar artefact;
# legacy state migration is disabled so the scan stays read-only.
def _fix_policy() -> dict:
    """Return the policy dict ``run_scan`` is invoked with under ``fix``.

    AC-7: literal lives here, not at the call site, so a future change
    is one-edit and the test contract pins the exact shape.
    """
    return {
        "index": {"embedding_sidecar": {"enabled": False}},
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
# argparse wiring
# ---------------------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``fix``'s flags onto an argparse (sub)parser.

    AC-3 / AC-4 / AC-5: ``--root`` is required and typed as ``Path``;
    ``--apply`` and ``--force`` are both ``store_true`` with default
    ``False``.
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

    # --- 1. Change detection (AC-6) --------------------------------------
    # detect() is invoked unconditionally with full_sweep=True so the fix
    # command sees every tracked *.py — there is no commit-range mode.
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

    # AC-8: only ``result.findings`` is consumed. ``schedule_decisions`` is
    # never read.
    # Audit S5: refuse to proceed if the analyzer emitted more findings
    # than the per-run cap. Done BEFORE the dedup pass so a pathological
    # scan cannot exhaust memory in the dedup set.
    if len(result.findings) > _MAX_FINDINGS_PER_RUN:
        print(
            f"autofix: refusing to process {len(result.findings)} findings "
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
    for f in result.findings:
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

    # --- 4. Classify findings + build per-file deletion plan -------------
    # We bucket findings by absolute file path so we can rewrite each file
    # exactly once, processing its safe deletions in descending start_line
    # order (AC-12).
    safe_by_file: dict[Path, list[tuple[int, str, str]]] = {}
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

    print(
        f"applied {applied} fix(es) across {applied_files} file(s); "
        f"{skipped_total} skipped "
        f"({unsafe_count} unsafe-multiname, {noqa_count} noqa)",
        file=sys.stderr,
    )
    return 0


__all__ = ["HELP_DESCRIPTION", "HELP_EPILOG", "add_arguments", "run"]
