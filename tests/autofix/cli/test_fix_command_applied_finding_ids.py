"""``_run_fix_core``'s ``applied_finding_ids`` set is actually populated.

Surfaced when running the dogfood demo:

* ``_run_fix_core`` returned ``FixCoreResult(applied_finding_ids=set())``
  even after the LLM patcher applied 4 unified diffs to the working tree.
* The crawl driver gates VERIFY + post-fix policy on
  ``len(fix_result.applied_finding_ids) > 0``. With the set always empty,
  the dispatcher early-returned at ``driver.py:369-370``, never invoked
  ``apply_post_fix_policy``, never branched, never opened a PR.
* Net effect: 4 real LLM-generated security/code-quality fixes landed
  in the working tree on every cycle, but the crawl looked like a no-op
  from the outside (no PR, no commit, no events visible in the log).

Both apply paths leak finding-ids:

1. The deterministic apply path (``safe_by_file`` rewrite) — never
   added to ``applied_finding_ids``.
2. The LLM apply path (``_run_llm_apply``) — never received the set.

This test pins the wire-up for both. If the set goes back to "always
empty after a successful apply", the dogfood demo silently regresses
to "applies fixes but never ships them" again.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from autofix.cli.fix_command import _run_fix_core, _run_llm_apply
from autofix.evidence.schema import CandidateFinding
from autofix.repair import RepairTier


def _git_init(tmp_path: Path, contents: dict[str, str]) -> None:
    """Init a git repo at ``tmp_path`` with the given files committed."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.invalid"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    for relpath, body in contents.items():
        p = tmp_path / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)


def test_run_llm_apply_populates_applied_finding_ids(tmp_path: Path) -> None:
    """``_run_llm_apply`` adds each successfully-applied finding's id to
    the caller-supplied set.
    """
    _git_init(tmp_path, {"a.py": "x = 1\n"})

    finding = MagicMock(
        rule_id="llm:security:command-injection",
        path="a.py",
        finding_id="llm:security:command-injection@a.py#L1-1",
    )
    task = MagicMock(finding=finding, tier=RepairTier.LLM_PATCH)

    fake_patch = MagicMock(
        patch_text=(
            "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        ),
        cache_hit=False,
    )

    applied: set[str] = set()
    with patch(
        "autofix.cli.fix_command.produce_patch", return_value=fake_patch
    ):
        result = _run_llm_apply(
            [task], tmp_path, max_llm_patches=None,
            applied_finding_ids=applied,
        )

    llm_applied, llm_attempted, llm_failed = result
    assert llm_applied == 1
    assert llm_failed == 0
    assert applied == {"llm:security:command-injection@a.py#L1-1"}


def test_run_llm_apply_does_not_add_id_when_apply_fails(
    tmp_path: Path,
) -> None:
    """A failing ``git apply`` must NOT add the id to the set —
    otherwise the driver runs VERIFY + post-fix on a finding that
    never landed in the tree.
    """
    _git_init(tmp_path, {"a.py": "x = 1\n"})
    finding = MagicMock(
        rule_id="llm:security:command-injection",
        path="a.py",
        finding_id="some-id",
    )
    task = MagicMock(finding=finding, tier=RepairTier.LLM_PATCH)

    # Patch text that does not match the file -> git apply rejects
    fake_patch = MagicMock(
        patch_text=(
            "--- a/a.py\n+++ b/a.py\n"
            "@@ -1 +1 @@\n"
            "-this line does not exist\n"
            "+replacement\n"
        ),
        cache_hit=False,
    )

    applied: set[str] = set()
    with patch(
        "autofix.cli.fix_command.produce_patch", return_value=fake_patch
    ):
        result = _run_llm_apply(
            [task], tmp_path, max_llm_patches=None,
            applied_finding_ids=applied,
        )

    llm_applied, llm_attempted, llm_failed = result
    assert llm_applied == 0
    assert llm_failed == 1
    assert applied == set()


def test_run_fix_core_returns_populated_applied_finding_ids_after_llm_apply(
    tmp_path: Path,
) -> None:
    """End-to-end: ``_run_fix_core`` with a working LLM apply path
    returns a NON-EMPTY ``applied_finding_ids`` so the crawl driver
    can decide to run VERIFY + post-fix policy.
    """
    _git_init(tmp_path, {"a.py": "x = 1\n"})

    finding = CandidateFinding(
        rule_id="llm:security:command-injection",
        path="a.py",
        symbol_name="cmd",
        normalized_import="",
        start_line=1,
        end_line=1,
        changed_slice="x = 1",
        finding_id="llm:security:command-injection@a.py#L1-1",
    )

    fake_patch = MagicMock(
        patch_text=(
            "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        ),
        cache_hit=False,
    )

    with patch(
        "autofix.cli.fix_command.produce_patch", return_value=fake_patch
    ):
        result = _run_fix_core(
            root=tmp_path,
            findings=[finding],
            apply_mode=True,
            suggest_mode=False,
            auto_llm=True,
            force=False,
            max_llm_patches=None,
            recovery_branch_already_captured=True,
            quiet=True,
        )

    assert result.exit_code == 0
    assert "llm:security:command-injection@a.py#L1-1" in result.applied_finding_ids, (
        f"expected the finding_id in the applied set; got "
        f"{result.applied_finding_ids!r} — without this the driver's "
        "applied_count == 0 gate skips VERIFY + post-fix policy and "
        "no PR is opened on a successful LLM apply"
    )
