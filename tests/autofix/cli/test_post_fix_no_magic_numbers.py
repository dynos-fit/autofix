"""No-magic-numbers grep contract for post_fix_policy.py (ARCH-015, AC-17)."""
from __future__ import annotations

import re
from pathlib import Path

POLICY_PATH = (
    Path(__file__).resolve().parents[3]
    / "autofix"
    / "cli"
    / "post_fix_policy.py"
)


def _body() -> str:
    src = POLICY_PATH.read_text(encoding="utf-8")
    # Strip module docstring + multi-line docstrings; the policy module
    # docstring documents the policy enum names, which would be a false
    # positive for the grep contract.
    return re.sub(r'"""[\s\S]*?"""', "", src, flags=re.MULTILINE)


def test_no_inline_policy_enum_strings() -> None:
    """The 3 enum strings must come from post_fix_constants, not inline."""
    src = _body()
    assert '"working-tree"' not in src
    assert '"branch-pr"' not in src
    # "branch" is harder — it appears inside path-like strings. Match a
    # standalone double-quoted "branch" literal only.
    assert re.search(r'(?<!-)"branch"(?!-)', src) is None


def test_no_inline_branch_prefix() -> None:
    """The branch name template lives in constants, not inline."""
    src = _body()
    assert "autofix/fixes-" not in src


def test_no_inline_commit_message_title_fragment() -> None:
    """Commit-message title fragment lives in constants."""
    src = _body()
    assert "autofix: applied" not in src


def test_no_inline_gh_pr_create_args() -> None:
    """The gh pr-create arg list is GH_PR_CREATE_BASE_ARGS, not inlined."""
    src = _body()
    # Standalone "pr" / "create" double-quoted literals must not appear.
    assert re.search(r'"pr"', src) is None
    assert re.search(r'"create"', src) is None
    assert re.search(r'"--fill"', src) is None


def test_no_inline_config_key() -> None:
    """The config key string lives in CONFIG_KEY_POST_FIX."""
    src = _body()
    assert '"post_fix"' not in src
