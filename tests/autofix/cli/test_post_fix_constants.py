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
    # ``--fill`` was dropped: PR title + body are now built explicitly
    # by post_fix_policy._build_pr_body.  ``--fill`` reused the bare
    # commit message + finding-id list, leaving reviewers nothing to
    # read above the diff.
    assert c.GH_PR_CREATE_BASE_ARGS == ("gh", "pr", "create")


def test_pr_body_template_contains_required_section_headings() -> None:
    """The PR body MUST carry the four canonical section headings.

    Mirrors the dynos-work PR-writing convention (Summary / Findings /
    Diff / Verify). If a section heading is removed without replacement,
    autofix's auto-opened PRs lose their "what + why + how to inspect"
    reading order and reviewers have to open the diff to learn anything.
    """
    for heading in (
        "## Summary",
        "## Findings fixed",
        "## Diff",
        "## Verify",
    ):
        assert heading in c.PR_BODY_TEMPLATE, (
            f"PR body template is missing required section: {heading!r}"
        )
    # Diff block must be a fenced ```diff fence so GitHub renders it
    # with diff-aware syntax highlighting.
    assert "```diff" in c.PR_BODY_TEMPLATE


def test_pr_finding_row_template_renders_correctly() -> None:
    rendered = c.PR_FINDING_ROW_TEMPLATE.format(
        rule_id="llm:security:command-injection",
        path="autofix/agent_loop.py",
        lines="137-154",
    )
    assert rendered == (
        "| `llm:security:command-injection` | "
        "`autofix/agent_loop.py:137-154` |"
    )


def test_pr_diff_max_bytes_under_github_body_cap() -> None:
    """GitHub PR bodies cap at 65_536 chars. Leave headroom for the
    surrounding template prose so the body never gets truncated by
    the API.
    """
    GITHUB_BODY_CAP = 65_536  # observed; not a public constant on the gh side
    assert c.PR_DIFF_MAX_BYTES < GITHUB_BODY_CAP
    assert c.PR_DIFF_MAX_BYTES > 0


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
        "PR_BODY_TEMPLATE",
        "PR_FINDING_ROW_TEMPLATE",
        "PR_DIFF_MAX_BYTES",
    }
    assert set(c.__all__) == expected
