"""Post-DONE branch-and-commit policy (ARCH-015).

After ``autofix run`` reaches :class:`~autofix.workflow.State.DONE`
with a non-empty applied-finding set, this module optionally
creates a fresh git branch and commits the applied fixes (and
optionally opens a PR via ``gh``). The policy is config-driven
via the project-level config file the verify primitive uses
(``CONFIG_PATH_RELATIVE`` from
:mod:`autofix.workflow.verify_constants`), with a CLI flag
override (``--post-fix``).

Three policy values are recognized
(:data:`autofix.cli.post_fix_constants.ALLOWED_POST_FIX`):

* ``working-tree`` — current behavior; no commit. (default)
* ``branch`` — create a branch, commit the working-tree
  changes, switch back to the original branch.
* ``branch-pr`` — ``branch`` + ``gh pr create`` with
  ``--fill``.

Graceful degradation
--------------------
Any subprocess error is caught; the function logs a warning to
stderr, best-effort restores the original branch, and returns
``working-tree`` as the policy actually applied. The function
NEVER raises into the caller — ``_run_one_cycle`` is exactly
this module's only call site, and a long-running daemon must
not crash on a broken git/gh interaction.

Working-tree blast radius
-------------------------
``branch`` and ``branch-pr`` both stage ALL working-tree changes
via ``git add -A``, not just the fix-applied paths. Operators
opting in must accept this trade — the autofix workflow controls
the state of the tree at the DONE transition (only the deletion
+ patch + verify pipeline has touched it), so an unrelated
commit drift is unlikely in practice.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from autofix.cli.post_fix_constants import (
    ALLOWED_POST_FIX,
    BRANCH_NAME_TEMPLATE,
    COMMIT_MESSAGE_BULLET_TEMPLATE,
    COMMIT_MESSAGE_TITLE_TEMPLATE,
    CONFIG_KEY_POST_FIX,
    DEFAULT_POST_FIX,
    GH_CLI_BIN,
    GH_PR_CREATE_BASE_ARGS,
    POST_FIX_BRANCH,
    POST_FIX_BRANCH_PR,
    POST_FIX_WORKING_TREE,
)
from autofix.workflow.verify_constants import CONFIG_PATH_RELATIVE


def _read_config(root: Path) -> dict | None:
    """Read the project config file if present and well-formed.

    Mirrors :func:`autofix.workflow.verify._read_config`'s
    fault-tolerant style: missing file, malformed JSON, and
    non-dict roots all return ``None``.
    """
    path = root / CONFIG_PATH_RELATIVE
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _resolve_policy(
    cli_override: str | None,
    config: dict | None,
    *,
    quiet: bool,
) -> str:
    """Resolve the active policy via CLI > config > default."""
    if cli_override is not None:
        if cli_override in ALLOWED_POST_FIX:
            return cli_override
        if not quiet:
            print(
                f"autofix: unknown --post-fix value {cli_override!r}; "
                f"using default",
                file=sys.stderr,
                flush=True,
            )
        return DEFAULT_POST_FIX

    if config is not None:
        raw = config.get(CONFIG_KEY_POST_FIX)
        if raw is None:
            return DEFAULT_POST_FIX
        if isinstance(raw, str) and raw in ALLOWED_POST_FIX:
            return raw
        if not quiet:
            print(
                f"autofix: unknown post_fix policy {raw!r}; using default",
                file=sys.stderr,
                flush=True,
            )
        return DEFAULT_POST_FIX

    return DEFAULT_POST_FIX


def _run_git(
    root: Path, args: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` inside ``root`` with text capture."""
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )


def _run_gh(
    root: Path, args: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    """Run ``gh <args>`` inside ``root`` with text capture.

    Argument list comes from
    :data:`autofix.cli.post_fix_constants.GH_PR_CREATE_BASE_ARGS`
    plus per-call ``--head`` / ``--base`` values. The literal
    string ``"gh"`` (and ``"pr"``, ``"create"``, ``"--fill"``)
    never appears inline in this module.
    """
    return subprocess.run(
        list(args),
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )


def _build_commit_message(
    *, run_id: str, applied_finding_ids: frozenset[str]
) -> tuple[str, str]:
    """Render (title, body) for the autofix commit message."""
    n = len(applied_finding_ids)
    title = COMMIT_MESSAGE_TITLE_TEMPLATE.format(n=n, run_id=run_id)
    bullets = "\n".join(
        COMMIT_MESSAGE_BULLET_TEMPLATE.format(finding_id=fid)
        for fid in sorted(applied_finding_ids)
    )
    return title, bullets


def _emit(msg: str, *, quiet: bool) -> None:
    if quiet:
        return
    print(msg, file=sys.stderr, flush=True)


def apply_post_fix_policy(
    *,
    root: Path,
    run_id: str,
    applied_finding_ids: frozenset[str],
    policy: str | None,
    quiet: bool,
) -> str:
    """Apply the resolved post-DONE policy and return the actual outcome.

    Parameters
    ----------
    root
        Repository root.
    run_id
        StateMachine run-id (used in branch name + commit message).
    applied_finding_ids
        Set of finding-ids whose fixes were applied during this
        run. An empty set short-circuits to a no-op regardless
        of policy.
    policy
        CLI override (``--post-fix``) or ``None``. When ``None``,
        falls back to config then default.
    quiet
        Suppress informational stderr lines.

    Returns
    -------
    str
        The policy actually applied (e.g.
        :data:`POST_FIX_WORKING_TREE` if degradation occurred).
    """
    if not applied_finding_ids:
        _emit(
            "autofix: post-fix → working-tree (empty applied set)",
            quiet=quiet,
        )
        return POST_FIX_WORKING_TREE

    config = _read_config(root)
    resolved = _resolve_policy(policy, config, quiet=quiet)

    if resolved == POST_FIX_WORKING_TREE:
        _emit(
            "autofix: post-fix → working-tree (no-op)",
            quiet=quiet,
        )
        return POST_FIX_WORKING_TREE

    branch_name = BRANCH_NAME_TEMPLATE.format(run_id=run_id)
    original_branch: str | None = None

    try:
        rev_parse = _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
        original_branch = rev_parse.stdout.strip()
        if not original_branch:
            raise RuntimeError("could not resolve current branch")

        _run_git(root, ["checkout", "-b", branch_name])
        _run_git(root, ["add", "-A"])

        title, body = _build_commit_message(
            run_id=run_id, applied_finding_ids=applied_finding_ids
        )
        _run_git(root, ["commit", "-m", title, "-m", body])

        _run_git(root, ["checkout", original_branch])

        if resolved == POST_FIX_BRANCH:
            _emit(
                f"autofix: post-fix → branch ({branch_name})",
                quiet=quiet,
            )
            return POST_FIX_BRANCH

        # POST_FIX_BRANCH_PR
        if shutil.which(GH_CLI_BIN) is None:
            _emit(
                "autofix: gh CLI not found; falling back to branch policy",
                quiet=quiet,
            )
            return POST_FIX_BRANCH

        pr_args = (
            *GH_PR_CREATE_BASE_ARGS,
            "--base",
            original_branch,
            "--head",
            branch_name,
        )
        _run_gh(root, pr_args)
        _emit(
            f"autofix: post-fix → branch-pr ({branch_name}, PR opened)",
            quiet=quiet,
        )
        return POST_FIX_BRANCH_PR

    except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
        _emit(
            f"autofix: post-fix policy {resolved} failed: {exc!r}; "
            f"reverting to working-tree",
            quiet=quiet,
        )
        if original_branch:
            try:
                _run_git(root, ["checkout", original_branch])
            except (subprocess.CalledProcessError, OSError):
                pass
        return POST_FIX_WORKING_TREE


__all__ = ["apply_post_fix_policy"]
