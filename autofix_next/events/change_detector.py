"""Git-based change detector for the autofix_next scan window.

The detector's job is to turn a repo root + a user flag into a
:class:`~autofix_next.events.schema.ChangeSet` of repo-relative
``*.py`` paths plus a ``watcher_confidence`` label that names the
strategy actually used. Three labels exist:

* ``"diff-head1"`` — ``git diff --name-only --no-renames HEAD~1 HEAD``
  filtered to ``*.py``. The default.
* ``"full-sweep"`` — ``git ls-files '*.py'``. Requested explicitly via
  ``--full-sweep``.
* ``"full-sweep-fallback"`` — ``git ls-files '*.py'`` triggered because
  ``HEAD~1`` does not exist (single-commit repo) and the default was
  requested (AC #6).

Working-tree modifications are deliberately NOT part of any changeset.
The diff range is strictly commit-to-commit. A caller that wants to
scan an in-progress edit must first commit it. This is the property
``--help`` advertises under AC #25.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from autofix_next.events.schema import ChangeSet


class NotAGitRepoError(RuntimeError):
    """Raised when the target directory is not inside a git working tree.

    The detector cannot infer a sensible scan window without git history,
    so this is a hard error surfaced to the caller (the CLI maps it to a
    non-zero exit code rather than silently full-sweeping).
    """


class GitUnavailableError(RuntimeError):
    """Raised when ``git`` is not installed / not on ``PATH``.

    Separated from :class:`NotAGitRepoError` so operators can distinguish
    "fix your environment" from "run ``git init`` first".
    """


def _run_git(
    root: Path,
    args: list[str],
    *,
    check: bool = False,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` inside ``root`` with captured text output.

    Parameters
    ----------
    root:
        CWD for the git subprocess.
    args:
        Argument vector, WITHOUT the leading ``"git"``.
    check:
        If ``True``, raises :class:`subprocess.CalledProcessError` on
        non-zero exit. Left ``False`` for the common "is there a HEAD~1?"
        probe where a non-zero return is expected and informative.
    timeout:
        Hard wall-clock ceiling (seconds). Defaults to 30s which is
        generous for any metadata query but prevents a wedged git from
        hanging the scan indefinitely.

    Returns
    -------
    subprocess.CompletedProcess[str]
        With ``stdout`` / ``stderr`` captured as ``str``.

    Raises
    ------
    GitUnavailableError
        If ``git`` is not installed (:class:`FileNotFoundError` from the
        executable lookup is translated here).
    subprocess.TimeoutExpired
        Propagated unchanged — the caller decides whether to retry.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
    except FileNotFoundError as exc:
        raise GitUnavailableError(
            "git executable not found on PATH; install git to run autofix-next"
        ) from exc


def _assert_is_git_repo(root: Path) -> None:
    """Raise :class:`NotAGitRepoError` unless ``root`` is inside a repo."""
    probe = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if probe.returncode != 0:
        stderr = (probe.stderr or "").lower()
        if "not a git repository" in stderr or probe.returncode == 128:
            raise NotAGitRepoError(
                f"{root} is not inside a git working tree "
                "(run 'git init' first, or pass --root to a real repo)"
            )
        # Any other failure: surface it so the operator can debug.
        raise NotAGitRepoError(
            f"git rev-parse failed in {root}: "
            f"returncode={probe.returncode} stderr={probe.stderr!r}"
        )


def _list_all_python_files(root: Path) -> tuple[str, ...]:
    """Return the tuple of tracked ``*.py`` paths via ``git ls-files``."""
    result = _run_git(root, ["ls-files", "*.py"])
    if result.returncode != 0:
        # Empty tree is not an error; an actual failure is.
        stderr = (result.stderr or "").lower()
        if "not a git repository" in stderr:
            raise NotAGitRepoError(
                f"{root} is not inside a git working tree"
            )
        raise RuntimeError(
            f"git ls-files failed: returncode={result.returncode} "
            f"stderr={result.stderr!r}"
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return tuple(lines)


def detect(root: Path, full_sweep: bool) -> tuple[ChangeSet, str]:
    """Compute the scan changeset for ``root``.

    Strategy:

    * ``full_sweep=True`` → list every tracked ``*.py`` via ``git ls-files``;
      label ``"full-sweep"``.
    * ``full_sweep=False`` → probe ``HEAD~1``. If it exists, run
      ``git diff --name-only --no-renames HEAD~1 HEAD``, filter to ``*.py``,
      label ``"diff-head1"``. If it doesn't (single-commit repo), fall
      back to listing every tracked ``*.py`` and label
      ``"full-sweep-fallback"`` (AC #6).

    Returns
    -------
    tuple[ChangeSet, str]
        The :class:`ChangeSet` AND the bare ``watcher_confidence`` string
        (also stored on the changeset). The redundant string is kept
        because the committed tests unpack a 2-tuple.

    Raises
    ------
    NotAGitRepoError
        ``root`` is not inside a git working tree.
    GitUnavailableError
        ``git`` is not available on ``PATH``.
    """
    root = Path(root)
    _assert_is_git_repo(root)

    if full_sweep:
        paths = _list_all_python_files(root)
        # AC #2: an explicit --full-sweep is NOT a fresh-instance event.
        # The operator asked for every tracked file, but the planner should
        # still use the incremental path (seeds + callers) — only the
        # fallback branch below signals "cold start".
        changeset = ChangeSet(
            paths=paths,
            watcher_confidence="full-sweep",
            is_fresh_instance=False,
        )
        return changeset, "full-sweep"

    # Probe HEAD~1 without --check so we can fall back instead of raising.
    probe = _run_git(root, ["rev-parse", "HEAD~1"])
    if probe.returncode != 0:
        # Most commonly: "fatal: ambiguous argument 'HEAD~1'" on a
        # single-commit repo. Fall back to the full sweep; AC #6 pins
        # the watcher_confidence label. AC #2: this is the only detector
        # branch that sets ``is_fresh_instance=True`` — a single-commit
        # repo has no prior state to diff against, so the planner should
        # take the bounded full-sweep fast path.
        paths = _list_all_python_files(root)
        changeset = ChangeSet(
            paths=paths,
            watcher_confidence="full-sweep-fallback",
            is_fresh_instance=True,
        )
        return changeset, "full-sweep-fallback"

    diff = _run_git(
        root,
        ["diff", "--name-only", "--no-renames", "HEAD~1", "HEAD"],
    )
    if diff.returncode != 0:
        raise RuntimeError(
            f"git diff HEAD~1 HEAD failed: returncode={diff.returncode} "
            f"stderr={diff.stderr!r}"
        )
    py_paths = tuple(
        line for line in diff.stdout.splitlines() if line.strip().endswith(".py")
    )
    # AC #2: ordinary diff-head1 is the incremental path, not fresh.
    changeset = ChangeSet(
        paths=py_paths,
        watcher_confidence="diff-head1",
        is_fresh_instance=False,
    )
    return changeset, "diff-head1"


__all__ = [
    "NotAGitRepoError",
    "GitUnavailableError",
    "detect",
]
