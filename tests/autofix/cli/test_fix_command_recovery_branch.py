"""TDD tests for ``autofix fix`` recovery-branch creation (AC-21 / AC-22.j).

Pinned ACs:
- AC-21.a: branch created via `git branch` with cwd=<root>, no -f flag
- AC-21.b: stdout line ``Recovery branch created: <name>. To revert: git reset --hard <name>``
- AC-21.c: collision retry with -1..-9 suffix
- AC-21.e: branch points at HEAD SHA at moment of creation, no checkout
- AC-21.f: ONLY for --apply --auto-llm; not for --apply alone, not for
  --suggest, not for --apply --suggest, not for bare invocation

These tests are RED — production code does not yet implement the recovery
branch. They will fail until ARCH-008 implementation lands.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)


def _commit(root: Path, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", msg],
        cwd=root,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-04-17T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-04-17T00:00:00Z",
        },
    )


def _head_sha(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _branch_sha(root: Path, branch_name: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", branch_name],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _list_branches(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "branch", "--list", "--format=%(refname:short)"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return [b for b in out.splitlines() if b]


def _list_recovery_branches(root: Path) -> list[str]:
    return [b for b in _list_branches(root) if b.startswith("autofix/pre-fix-snapshot-")]


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

def _make_llm_patch(*, finding_id: str, path: str, body: str):
    from autofix.repair import LLMPatch
    return LLMPatch(
        finding_id=finding_id, file_path=path, patch_text=body,
        model="opus", cache_hit=False, hunk_count=1,
    )


def _make_repair_task(*, finding_id: str, path: str, rule_id: str = "linter:mypy:foo"):
    from autofix.evidence.schema import CandidateFinding
    from autofix.repair import RepairTask, RepairTier
    f = CandidateFinding(
        rule_id=rule_id, path=path, symbol_name="", normalized_import="",
        start_line=1, end_line=1, changed_slice="", finding_id=finding_id,
    )
    return RepairTask(finding=f, tier=RepairTier.LLM_PATCH, reason="prefix_mapped")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_with_findings(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    (tmp_path / "module.py").write_text("import os\n\nx = 1\n", encoding="utf-8")
    _commit(tmp_path, "init")
    return tmp_path


# ---------------------------------------------------------------------------
# AC-22.j primary: --apply --auto-llm creates a branch matching the prefix
# pointing at the pre-run HEAD SHA. Stdout has the AC-21.b line.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_apply_auto_llm_creates_recovery_branch(
    repo_with_findings: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-21 / AC-22.j: --apply --auto-llm creates `autofix/pre-fix-snapshot-*`."""
    from autofix.cli import fix_command

    pre_run_head = _head_sha(repo_with_findings)
    branches_before = set(_list_recovery_branches(repo_with_findings))
    assert branches_before == set(), "Test invariant: no recovery branches before run"

    # Stub coordinator to return zero LLM_PATCH tasks so we don't need a real
    # apply-able diff. The recovery branch must still be created.
    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: [],
        raising=False,
    )

    ns = argparse.Namespace(
        root=repo_with_findings, apply=True, force=False,
        suggest=False, auto_llm=True, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 0, f"Expected rc=0, got {rc}. Stderr:\n{captured.err}"

    # AC-21.a: a recovery branch was created.
    branches_after = _list_recovery_branches(repo_with_findings)
    assert len(branches_after) == 1, (
        f"Exactly one recovery branch expected. Got: {branches_after!r}"
    )
    branch_name = branches_after[0]

    # AC-21: name matches the prefix + compact UTC timestamp (no `:` since
    # `git check-ref-format` rejects colons in ref names).
    name_re = re.compile(
        r"^autofix/pre-fix-snapshot-\d{8}T\d{6}Z(-\d)?$"
    )
    assert name_re.match(branch_name), (
        f"Branch name must match prefix+compact-UTC-timestamp pattern. Got: {branch_name!r}"
    )

    # AC-21.e: branch points at the pre-run HEAD SHA.
    assert _branch_sha(repo_with_findings, branch_name) == pre_run_head, (
        f"AC-21.e: recovery branch must point at pre-run HEAD ({pre_run_head}); "
        f"got {_branch_sha(repo_with_findings, branch_name)}"
    )

    # AC-21.b: stdout has the announcement line with the chosen branch name.
    expected_line = f"Recovery branch created: {branch_name}. To revert: git reset --hard {branch_name}"
    assert expected_line in captured.out, (
        f"AC-21.b: expected stdout line {expected_line!r}. Got:\n{captured.out}"
    )


# ---------------------------------------------------------------------------
# AC-21.f: --apply alone (no --auto-llm) must NOT create a recovery branch.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_apply_alone_does_not_create_recovery_branch(
    repo_with_findings: Path, capsys: pytest.CaptureFixture,
) -> None:
    """AC-21.f: bare --apply (deterministic-only) does not create a branch."""
    from autofix.cli import fix_command

    ns = argparse.Namespace(
        root=repo_with_findings, apply=True, force=False,
        suggest=False, auto_llm=False, max_llm_patches=None,
    )
    fix_command.run(ns)
    capsys.readouterr()

    branches = _list_recovery_branches(repo_with_findings)
    assert branches == [], (
        f"AC-21.f: --apply alone must not plant a recovery branch. Got: {branches!r}"
    )


# ---------------------------------------------------------------------------
# AC-21.f: --suggest alone (no --apply, no --auto-llm) → no branch.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_suggest_only_does_not_create_recovery_branch(
    repo_with_findings: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-21.f: --suggest alone (no --apply) does not create a branch."""
    from autofix.cli import fix_command

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: [],
        raising=False,
    )

    ns = argparse.Namespace(
        root=repo_with_findings, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=None,
    )
    fix_command.run(ns)
    capsys.readouterr()

    branches = _list_recovery_branches(repo_with_findings)
    assert branches == [], (
        f"AC-21.f: --suggest alone must not plant a recovery branch. Got: {branches!r}"
    )


# ---------------------------------------------------------------------------
# AC-21.f: --apply --suggest (deterministic apply + preview) → no branch.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_apply_and_suggest_does_not_create_recovery_branch(
    repo_with_findings: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-21.f: --apply --suggest does not create a branch."""
    from autofix.cli import fix_command

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: [],
        raising=False,
    )

    ns = argparse.Namespace(
        root=repo_with_findings, apply=True, force=False,
        suggest=True, auto_llm=False, max_llm_patches=None,
    )
    fix_command.run(ns)
    capsys.readouterr()

    branches = _list_recovery_branches(repo_with_findings)
    assert branches == [], (
        f"AC-21.f: --apply --suggest must not plant a recovery branch. Got: {branches!r}"
    )


# ---------------------------------------------------------------------------
# AC-21.f: bare ``autofix fix`` (dry-run) → no branch.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_bare_fix_does_not_create_recovery_branch(
    repo_with_findings: Path, capsys: pytest.CaptureFixture,
) -> None:
    """AC-21.f: bare invocation (dry-run, no flags) does not create a branch."""
    from autofix.cli import fix_command

    ns = argparse.Namespace(
        root=repo_with_findings, apply=False, force=False,
        suggest=False, auto_llm=False, max_llm_patches=None,
    )
    fix_command.run(ns)
    capsys.readouterr()

    branches = _list_recovery_branches(repo_with_findings)
    assert branches == [], (
        f"AC-21.f: bare fix must not plant a recovery branch. Got: {branches!r}"
    )


# ---------------------------------------------------------------------------
# AC-21.c: pre-existing branch with the wall-clock timestamp → suffix -1
# is created.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_recovery_branch_collision_retry_uses_suffix(
    repo_with_findings: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-21.c: timestamp collision → -1 suffix appended.

    Pin the canonical timestamp by monkeypatching the datetime source.
    Pre-create the un-suffixed branch; the implementation must pick -1.
    """
    from autofix.cli import fix_command

    pinned_ts = "20260507T123456Z"
    pre_existing = f"autofix/pre-fix-snapshot-{pinned_ts}"

    # Pre-create the colliding branch.
    subprocess.run(
        ["git", "branch", pre_existing, "HEAD"],
        cwd=repo_with_findings, check=True, capture_output=True,
    )

    # Pin the timestamp by monkeypatching `datetime.now` inside fix_command.
    # The implementation imports `from datetime import datetime, timezone`
    # at module top, so we patch the bound name on the module.
    import datetime as _dt
    real_dt = _dt.datetime

    class _PinnedDateTime(real_dt):
        @classmethod
        def now(cls, tz=None):
            # Construct a tz-aware datetime that strftime("%Y%m%dT%H%M%SZ")
            # yields the pinned_ts when tz is utc.
            return real_dt(2026, 5, 7, 12, 34, 56, tzinfo=tz or _dt.timezone.utc)

    # Patch on fix_command module since it uses `datetime.now(timezone.utc)`.
    monkeypatch.setattr(fix_command, "datetime", _PinnedDateTime, raising=False)

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: [],
        raising=False,
    )

    ns = argparse.Namespace(
        root=repo_with_findings, apply=True, force=False,
        suggest=False, auto_llm=True, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 0, f"Expected rc=0 even with collision retry. Got rc={rc}. Stderr:\n{captured.err}"

    branches = _list_recovery_branches(repo_with_findings)
    # Both the pre-existing branch and the suffix-1 branch should exist.
    assert pre_existing in branches, (
        f"Pre-existing branch must remain. Got: {branches!r}"
    )
    expected_suffix_branch = f"autofix/pre-fix-snapshot-{pinned_ts}-1"
    assert expected_suffix_branch in branches, (
        f"AC-21.c: collision retry must create {expected_suffix_branch!r}. Got: {branches!r}"
    )
    # AC-21.b: stdout names the chosen (suffix) branch, not the pre-existing one.
    assert expected_suffix_branch in captured.out, (
        f"AC-21.b: stdout must announce the chosen branch {expected_suffix_branch!r}. "
        f"Got:\n{captured.out}"
    )
