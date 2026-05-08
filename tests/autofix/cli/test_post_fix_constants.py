"""Constants-shape tests for ARCH-015 (AC-1, AC-18)."""
from __future__ import annotations

from autofix.cli import post_fix_constants as c


def test_policy_enum_strings() -> None:
    assert c.POST_FIX_WORKING_TREE == "working-tree"
    assert c.POST_FIX_BRANCH == "branch"
    assert c.POST_FIX_BRANCH_PR == "branch-pr"


def test_default_policy() -> None:
    assert c.DEFAULT_POST_FIX == c.POST_FIX_WORKING_TREE


def test_allowed_policies_in_canonical_order() -> None:
    assert c.ALLOWED_POST_FIX == (
        c.POST_FIX_WORKING_TREE,
        c.POST_FIX_BRANCH,
        c.POST_FIX_BRANCH_PR,
    )


def test_branch_name_template() -> None:
    rendered = c.BRANCH_NAME_TEMPLATE.format(run_id="abc123")
    assert rendered == "autofix/fixes-abc123"


def test_commit_message_title_template() -> None:
    rendered = c.COMMIT_MESSAGE_TITLE_TEMPLATE.format(n=3, run_id="r-x")
    assert rendered == "autofix: applied 3 fixes (run r-x)"


def test_commit_message_bullet_template() -> None:
    rendered = c.COMMIT_MESSAGE_BULLET_TEMPLATE.format(finding_id="F-1")
    assert rendered == "- finding-id: F-1"


def test_config_key() -> None:
    assert c.CONFIG_KEY_POST_FIX == "post_fix"


def test_gh_args() -> None:
    assert c.GH_CLI_BIN == "gh"
    assert c.GH_PR_CREATE_BASE_ARGS == ("gh", "pr", "create", "--fill")


def test_all_exports() -> None:
    expected = {
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
    }
    assert set(c.__all__) == expected
