"""Constants for the post-DONE branch-and-commit policy (ARCH-015).

Side-effect-free module: every string literal the post_fix_policy
module uses lives here. The policy module body MUST NOT inline
these values — the no-magic-numbers grep test
(:mod:`tests.autofix.cli.test_post_fix_no_magic_numbers`) treats
inline literals as a binding contract violation.

Mirrors the discipline established in
:mod:`autofix.cli.run_constants` and
:mod:`autofix.workflow.verify_constants`.
"""
from __future__ import annotations


# --- Policy enum values ----------------------------------------------------

POST_FIX_WORKING_TREE: str = "working-tree"
POST_FIX_BRANCH: str = "branch"
POST_FIX_BRANCH_PR: str = "branch-pr"

DEFAULT_POST_FIX: str = POST_FIX_WORKING_TREE

ALLOWED_POST_FIX: tuple[str, ...] = (
    POST_FIX_WORKING_TREE,
    POST_FIX_BRANCH,
    POST_FIX_BRANCH_PR,
)


# --- Branch / commit templates ---------------------------------------------

BRANCH_NAME_TEMPLATE: str = "autofix/fixes-{run_id}"
COMMIT_MESSAGE_TITLE_TEMPLATE: str = (
    "autofix: applied {n} fixes (run {run_id})"
)
COMMIT_MESSAGE_BULLET_TEMPLATE: str = "- finding-id: {finding_id}"


# --- Config wiring ---------------------------------------------------------

CONFIG_KEY_POST_FIX: str = "post_fix"


# --- Subprocess argument lists ---------------------------------------------

GH_CLI_BIN: str = "gh"

# `gh pr create` invocation. The `--head` and `--base` flags are
# appended at call-time; the literal arg-list lives here so the
# policy module never inlines `"pr"`/`"create"`/`"--fill"`.
GH_PR_CREATE_BASE_ARGS: tuple[str, ...] = (
    GH_CLI_BIN,
    "pr",
    "create",
    "--fill",
)


__all__ = [
    "POST_FIX_WORKING_TREE",
    "POST_FIX_BRANCH",
    "POST_FIX_BRANCH_PR",
    "DEFAULT_POST_FIX",
    "ALLOWED_POST_FIX",
    "BRANCH_NAME_TEMPLATE",
    "COMMIT_MESSAGE_TITLE_TEMPLATE",
    "COMMIT_MESSAGE_BULLET_TEMPLATE",
    "CONFIG_KEY_POST_FIX",
    "GH_CLI_BIN",
    "GH_PR_CREATE_BASE_ARGS",
]
