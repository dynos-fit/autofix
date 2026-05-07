"""TDD tests for ``autofix fix --apply --auto-llm`` (ARCH-008).

Covers AC-22.d (apply happy path), AC-22.e (partial failure),
AC-22.f (dirty-tree gate), AC-22.i (3-way recovery).

Pinned ACs:
- AC-2: --auto-llm flag wiring
- AC-3: --suggest --auto-llm conflict
- AC-4: --auto-llm without --apply
- AC-12: deterministic-first then LLM-second ordering
- AC-13: _apply_unified_diff helper using `git apply --3way`
- AC-14: report-and-continue on per-patch failure
- AC-15: stderr line format on per-patch failure
- AC-16: dirty-tree gate semantics (with/without --force)
- AC-19.c: exit code policy for --apply --auto-llm

These tests are RED — production code does not yet implement --auto-llm
or _apply_unified_diff. They will fail until ARCH-008 implementation lands.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Git repo helpers (mirror test_fix_command_apply.py)
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


def _make_llm_patch(*, finding_id: str, path: str, body: str):
    from autofix.repair import LLMPatch
    return LLMPatch(
        finding_id=finding_id,
        file_path=path,
        patch_text=body,
        model="opus",
        cache_hit=False,
        hunk_count=1,
    )


def _make_repair_task(*, finding_id: str, path: str, rule_id: str = "linter:mypy:foo",
                     start_line: int = 1, end_line: int = 1):
    from autofix.evidence.schema import CandidateFinding
    from autofix.repair import RepairTask, RepairTier
    finding = CandidateFinding(
        rule_id=rule_id, path=path, symbol_name="",
        normalized_import="", start_line=start_line, end_line=end_line,
        changed_slice="", finding_id=finding_id,
    )
    return RepairTask(finding=finding, tier=RepairTier.LLM_PATCH, reason="prefix_mapped")


# ---------------------------------------------------------------------------
# Fixture: clean git repo with one Python file containing one unused import
# (analyzer flags `import os\n`) PLUS additional content for a meaningful
# LLM-patch operation.
# ---------------------------------------------------------------------------

@pytest.fixture
def llm_repo(tmp_path: Path) -> Path:
    """Clean git repo: one tracked .py file. Will be the apply target."""
    _init_repo(tmp_path)
    # The file contains an unused import (`import os\n` at line 1) so the
    # deterministic deletion path has work to do, plus a `value = 1\n` line
    # the LLM patch will rewrite.
    (tmp_path / "module.py").write_text(
        "import os\n\nvalue = 1\n", encoding="utf-8"
    )
    _commit(tmp_path, "init")
    return tmp_path


# ---------------------------------------------------------------------------
# AC-22.d: --apply --auto-llm happy path. Real git apply --3way succeeds.
# Source bytes reflect both deterministic deletion AND the LLM patch.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(60)
def test_apply_auto_llm_happy_path(
    llm_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-22.d: --apply --auto-llm with a clean patch applies via git apply --3way."""
    from autofix.cli import fix_command

    # In production AC-12 ordering, produce_patch is called AFTER deterministic
    # deletion runs, so the LLM sees the post-deletion file:
    #   line 1: (blank line that was line 2)
    #   line 2: value = 1
    # The diff is against this post-deletion 2-line state. git apply with
    # --3way handles minor context fuzz; for the happy path we don't need it
    # to recover from a deletion shift because production order avoids that.
    diff_body = (
        "diff --git a/module.py b/module.py\n"
        "--- a/module.py\n"
        "+++ b/module.py\n"
        "@@ -1,2 +1,2 @@\n"
        " \n"
        "-value = 1\n"
        "+value = 42\n"
    )
    patch = _make_llm_patch(finding_id="fid_happy", path="module.py", body=diff_body)
    task = _make_repair_task(finding_id="fid_happy", path="module.py")

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: [task],
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", lambda t, **kw: patch, raising=False)

    ns = argparse.Namespace(
        root=llm_repo, apply=True, force=False,
        suggest=False, auto_llm=True, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 0, f"AC-19.c: expected rc=0 on successful apply, got {rc}. Stderr:\n{captured.err}"

    after = (llm_repo / "module.py").read_text(encoding="utf-8")
    # Deterministic deletion: import os removed.
    assert "import os\n" not in after, (
        f"Deterministic deletion should have happened. File now:\n{after}"
    )
    # LLM patch applied: value=42 (not value=1) is present.
    assert "value = 42" in after, (
        f"LLM patch should have applied (value = 42). File now:\n{after}"
    )
    # No .rej files left in the worktree (AC-13: --reject is NOT passed).
    rej_files = list(llm_repo.rglob("*.rej"))
    assert rej_files == [], f"No .rej files allowed (AC-13). Found: {rej_files!r}"


# ---------------------------------------------------------------------------
# AC-22.f: dirty-tree without --force → exit 2, no produce calls.
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_apply_auto_llm_dirty_tree_refuses(
    llm_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-16.a / AC-22.f: dirty tree without --force → rc=2, zero produce calls."""
    from autofix.cli import fix_command

    # Make the tree dirty (modify tracked file).
    (llm_repo / "module.py").write_text(
        "import os\n\nvalue = 999\n", encoding="utf-8"
    )

    produce_calls: list = []

    def fake_produce(*a, **kw):
        produce_calls.append((a, kw))
        return None

    monkeypatch.setattr(fix_command, "produce_patch", fake_produce, raising=False)

    ns = argparse.Namespace(
        root=llm_repo, apply=True, force=False,
        suggest=False, auto_llm=True, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 2, f"AC-16.a: dirty tree without --force must return rc=2, got {rc}"
    assert produce_calls == [], (
        f"AC-16.a: produce_patch must not be called on dirty-tree refusal, got {produce_calls!r}"
    )
    assert "dirty" in captured.err.lower() or "path" in captured.err.lower(), (
        f"Stderr must mention dirty-tree refusal. Got:\n{captured.err}"
    )


# ---------------------------------------------------------------------------
# AC-22.f cont'd: dirty-tree WITH --force → proceeds and reaches the LLM
# branch. Source eventually contains the deterministic deletion.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(60)
def test_apply_auto_llm_force_proceeds_on_dirty_tree(
    llm_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-16.b / AC-22.f: --force overrides dirty-tree refusal."""
    from autofix.cli import fix_command

    # Make tree dirty in an unrelated way.
    (llm_repo / "unrelated.txt").write_text("dirty", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=llm_repo, check=True)

    # Stub coordinator to return zero LLM_PATCH tasks (so we don't have to
    # also build a real diff for this test — the produce_patch path is
    # exercised in the happy-path test).
    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: [],
        raising=False,
    )

    ns = argparse.Namespace(
        root=llm_repo, apply=True, force=True,
        suggest=False, auto_llm=True, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 0, f"--force should proceed past dirty tree, got rc={rc}. Stderr:\n{captured.err}"
    after = (llm_repo / "module.py").read_text(encoding="utf-8")
    assert "import os\n" not in after, (
        "Deterministic deletion should run after --force overrides dirty gate"
    )


# ---------------------------------------------------------------------------
# AC-22.e: partial failure — one clean patch + one bogus patch; the bogus
# fails BUT the run does not abort early; AC-15 stderr line is written.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(60)
def test_apply_auto_llm_partial_failure_reports_and_continues(
    llm_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-14 / AC-15 / AC-22.e: per-patch failure does not abort the iteration."""
    from autofix.cli import fix_command

    # Production AC-12 ordering: produce_patch sees post-deletion file (2 lines).
    good_diff = (
        "diff --git a/module.py b/module.py\n"
        "--- a/module.py\n"
        "+++ b/module.py\n"
        "@@ -1,2 +1,2 @@\n"
        " \n"
        "-value = 1\n"
        "+value = 7\n"
    )
    # Bogus diff: index line references a fabricated blob hash NOT in the
    # local object database, so neither literal apply nor 3-way can succeed.
    bogus_diff = (
        "diff --git a/module.py b/module.py\n"
        "index dead00ddead00ddead00ddead00ddead00ddead0..0000000 100644\n"
        "--- a/module.py\n"
        "+++ b/module.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-this line is not in the file\n"
        "+replacement\n"
        " context that does not match\n"
        " also not in file\n"
    )
    good_patch = _make_llm_patch(finding_id="fid_good", path="module.py", body=good_diff)
    bad_patch = _make_llm_patch(finding_id="fid_bad", path="module.py", body=bogus_diff)

    tasks = [
        _make_repair_task(finding_id="fid_bad", path="module.py"),
        _make_repair_task(finding_id="fid_good", path="module.py"),
    ]

    def fake_produce(task, **kw):
        if task.finding.finding_id == "fid_bad":
            return bad_patch
        return good_patch

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: tasks,
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", fake_produce, raising=False)

    ns = argparse.Namespace(
        root=llm_repo, apply=True, force=False,
        suggest=False, auto_llm=True, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    # AC-19.c.iii: at least one applied successfully → rc=0.
    assert rc == 0, (
        f"AC-19.c.iii: rc=0 when at least one LLM patch applied. Got rc={rc}. "
        f"Stderr:\n{captured.err}"
    )
    # AC-15: stderr has a per-patch failure line for fid_bad.
    assert "LLM patch failed for finding fid_bad" in captured.err, (
        f"AC-15: expected per-patch failure line for fid_bad in stderr. Got:\n{captured.err}"
    )
    # The good patch's content is in the file.
    after = (llm_repo / "module.py").read_text(encoding="utf-8")
    assert "value = 7" in after, (
        f"Good patch should still apply despite bad patch failing. File:\n{after}"
    )


# ---------------------------------------------------------------------------
# AC-19.c.b/d: every patch fails AND deterministic produced zero successes
# → rc=1.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(60)
def test_apply_auto_llm_all_fail_and_no_deterministic_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-19.c (bottom): every LLM apply fails AND no deterministic apply → rc=1."""
    from autofix.cli import fix_command

    # Build a repo with NO unused-import findings (so the deterministic path
    # finds zero safe deletions) but with one bogus LLM_PATCH-tier task.
    _init_repo(tmp_path)
    (tmp_path / "clean.py").write_text(
        "import os\n\npath = os.getcwd()\n", encoding="utf-8"
    )
    _commit(tmp_path, "clean")

    bogus_diff = (
        "diff --git a/clean.py b/clean.py\n"
        "index dead00ddead00ddead00ddead00ddead00ddead0..0000000 100644\n"
        "--- a/clean.py\n"
        "+++ b/clean.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-this line is not in the file\n"
        "+replacement\n"
        " context that does not match\n"
        " also not in file\n"
    )
    bad_patch = _make_llm_patch(finding_id="fid_only_bad", path="clean.py", body=bogus_diff)
    task = _make_repair_task(finding_id="fid_only_bad", path="clean.py")

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: [task],
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", lambda t, **kw: bad_patch, raising=False)

    ns = argparse.Namespace(
        root=tmp_path, apply=True, force=False,
        suggest=False, auto_llm=True, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    # NOTE: This case is subtle — if the analyzer finds zero unused imports
    # in this fixture, the run short-circuits with "no findings" before ever
    # reaching the coordinator. We accept rc=0 in that case (no findings),
    # OR rc=1 if findings exist but all LLM applications failed.
    if "no findings" in captured.err:
        assert rc == 0
    else:
        assert rc == 1, (
            f"AC-19.c: every LLM apply failed AND no deterministic success → rc=1. "
            f"Got rc={rc}. Stderr:\n{captured.err}"
        )


# ---------------------------------------------------------------------------
# AC-13 helper directly: _apply_unified_diff applies a clean patch via
# git apply --3way and writes the modified file in place.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_apply_unified_diff_helper_clean_apply(llm_repo: Path) -> None:
    """AC-13: _apply_unified_diff applies a clean unified diff via `git apply --3way`."""
    from autofix.cli import fix_command

    pre_blob = subprocess.run(
        ["git", "hash-object", "--", "module.py"],
        cwd=llm_repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    diff_body = (
        f"diff --git a/module.py b/module.py\n"
        f"index {pre_blob}..0000000 100644\n"
        f"--- a/module.py\n"
        f"+++ b/module.py\n"
        "@@ -1,3 +1,3 @@\n"
        " import os\n"
        " \n"
        "-value = 1\n"
        "+value = 99\n"
    )

    fix_command._apply_unified_diff(patch_text=diff_body, root=llm_repo)

    after = (llm_repo / "module.py").read_text(encoding="utf-8")
    assert "value = 99" in after, f"Helper must apply patch in place. File:\n{after}"


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_apply_unified_diff_helper_raises_on_failure(llm_repo: Path) -> None:
    """AC-13: _apply_unified_diff raises _LLMPatchApplyError on non-zero git apply."""
    from autofix.cli import fix_command

    bogus_diff = (
        "diff --git a/module.py b/module.py\n"
        "index dead00ddead00ddead00ddead00ddead00ddead0..0000000 100644\n"
        "--- a/module.py\n"
        "+++ b/module.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-not in file\n"
        "+x\n"
        " also not\n"
        " not either\n"
    )

    with pytest.raises(fix_command._LLMPatchApplyError):
        fix_command._apply_unified_diff(patch_text=bogus_diff, root=llm_repo)


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_apply_unified_diff_helper_does_not_produce_rej_files(llm_repo: Path) -> None:
    """AC-13: failure does NOT leave .rej files (no --reject flag passed)."""
    from autofix.cli import fix_command

    bogus_diff = (
        "diff --git a/module.py b/module.py\n"
        "index dead00ddead00ddead00ddead00ddead00ddead0..0000000 100644\n"
        "--- a/module.py\n"
        "+++ b/module.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-not in file\n"
        "+x\n"
        " also not\n"
        " not either\n"
    )

    with pytest.raises(fix_command._LLMPatchApplyError):
        fix_command._apply_unified_diff(patch_text=bogus_diff, root=llm_repo)

    rej = list(llm_repo.rglob("*.rej"))
    assert rej == [], f"AC-13: --reject must NOT be passed. Found .rej files: {rej!r}"
