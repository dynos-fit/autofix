"""TDD tests for ``autofix fix --suggest`` (ARCH-008 task-20260507-010).

Covers AC-22.a (preview happy path), AC-22.b (conflicting flags),
AC-22.c (missing prerequisite), AC-22.g (no-events invariant).

Pinned ACs:
- AC-1, AC-3, AC-4: argparse flag wiring + usage error gates
- AC-5, AC-6: preview-only / apply+suggest accepted invocations
- AC-7, AC-8, AC-9: coordinator call shape + threshold + produce_patch
- AC-10: 4-line preview-block format
- AC-11: rejection one-liner format
- AC-17: no-events-from-fix invariant
- AC-19.a: exit code 0 for preview-only success

These tests are written RED — production code does not yet implement
``--suggest`` or ``--auto-llm``. Tests will fail until ARCH-008
implementation lands. That is correct TDD-RED state.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Git repo bootstrap helpers (mirror test_fix_command_apply.py exactly)
# ---------------------------------------------------------------------------

def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=root, check=True
    )


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


# ---------------------------------------------------------------------------
# Stub helpers (canned LLMPatch / fake produce_patch)
# ---------------------------------------------------------------------------

def _make_llm_patch(*, finding_id: str, path: str, body: str):
    """Construct an LLMPatch with the given fields. cache_hit=False, opus."""
    from autofix.repair import LLMPatch
    return LLMPatch(
        finding_id=finding_id,
        file_path=path,
        patch_text=body,
        model="opus",
        cache_hit=False,
        hunk_count=1,
    )


def _make_repair_task(*, finding_id: str, path: str, rule_id: str = "linter:mypy:type-error",
                     start_line: int = 1, end_line: int = 1):
    """Construct a RepairTask whose tier is LLM_PATCH."""
    from autofix.evidence.schema import CandidateFinding
    from autofix.repair import RepairTask, RepairTier
    finding = CandidateFinding(
        rule_id=rule_id,
        path=path,
        symbol_name="",
        normalized_import="",
        start_line=start_line,
        end_line=end_line,
        changed_slice="",
        finding_id=finding_id,
    )
    return RepairTask(finding=finding, tier=RepairTier.LLM_PATCH, reason="prefix_mapped")


# ---------------------------------------------------------------------------
# Fixture: repo with one Python file that yields one unused-import finding.
# ---------------------------------------------------------------------------

@pytest.fixture
def basic_repo(tmp_path: Path) -> Path:
    """Real git repo with one tracked .py file that has one unused import."""
    _init_repo(tmp_path)
    (tmp_path / "module.py").write_text("import os\n\nx = 1\n", encoding="utf-8")
    _commit(tmp_path, "init")
    return tmp_path


# ---------------------------------------------------------------------------
# AC-3: --suggest + --auto-llm together → non-zero exit, usage error stderr,
# zero LLM invocations, zero source mutations, AND error path runs BEFORE
# detect()/run_scan() (so the analyzer is not invoked).
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_suggest_and_auto_llm_together_rejected(
    basic_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3 / AC-22.b: --suggest --auto-llm together exits non-zero with usage error."""
    from autofix.cli import fix_command

    produce_calls: list = []
    detect_calls: list = []
    scan_calls: list = []

    def fake_produce(*a, **kw):
        produce_calls.append((a, kw))
        return None

    def fake_detect(*a, **kw):
        detect_calls.append((a, kw))
        return ([], 1.0)

    def fake_scan(*a, **kw):
        scan_calls.append((a, kw))
        raise AssertionError("run_scan must not be called on usage error")

    # Patch at the import site inside fix_command (per spec assumption).
    monkeypatch.setattr(fix_command, "produce_patch", fake_produce, raising=False)
    monkeypatch.setattr(fix_command, "detect", fake_detect, raising=False)
    monkeypatch.setattr(fix_command, "run_scan", fake_scan, raising=False)

    file_before = (basic_repo / "module.py").read_bytes()

    ns = argparse.Namespace(
        root=basic_repo, apply=False, force=False,
        suggest=True, auto_llm=True, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc != 0, f"Expected non-zero exit, got {rc}"
    assert produce_calls == [], "produce_patch must not be called on usage error"
    assert detect_calls == [], "detect() must not be called before usage validation"
    # Stderr names both flags by their long-form spelling and explains exclusivity.
    assert "--suggest" in captured.err and "--auto-llm" in captured.err, (
        f"Stderr must name both flag spellings. Got:\n{captured.err}"
    )
    assert "mutually exclusive" in captured.err.lower() or "cannot be combined" in captured.err.lower() \
        or "exclusive" in captured.err.lower(), (
        f"Stderr must explain the flags are mutually exclusive. Got:\n{captured.err}"
    )
    # Source unchanged.
    assert (basic_repo / "module.py").read_bytes() == file_before


# ---------------------------------------------------------------------------
# AC-4: --auto-llm without --apply → non-zero exit, usage error stderr,
# zero LLM invocations, error before detect()/run_scan().
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_auto_llm_without_apply_rejected(
    basic_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-4 / AC-22.c: --auto-llm without --apply exits non-zero with usage error."""
    from autofix.cli import fix_command

    produce_calls: list = []
    detect_calls: list = []

    def fake_produce(*a, **kw):
        produce_calls.append((a, kw))
        return None

    def fake_detect(*a, **kw):
        detect_calls.append((a, kw))
        return ([], 1.0)

    monkeypatch.setattr(fix_command, "produce_patch", fake_produce, raising=False)
    monkeypatch.setattr(fix_command, "detect", fake_detect, raising=False)

    ns = argparse.Namespace(
        root=basic_repo, apply=False, force=False,
        suggest=False, auto_llm=True, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc != 0, f"Expected non-zero exit, got {rc}"
    assert produce_calls == []
    assert detect_calls == [], "detect() must not run before usage validation"
    assert "--auto-llm" in captured.err
    assert "--apply" in captured.err
    assert "requires" in captured.err.lower() or "needs" in captured.err.lower() \
        or "without" in captured.err.lower(), (
        f"Stderr must indicate --auto-llm requires --apply. Got:\n{captured.err}"
    )


# ---------------------------------------------------------------------------
# AC-22.a happy path: --suggest only → AC-10 4-line preview block on stdout
# for a non-None LLMPatch return; source byte-identical; rc=0 (AC-19.a).
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_suggest_only_prints_preview_block(
    basic_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-10 / AC-22.a: bare --suggest prints the 4-line preview block on stdout.

    The block layout per AC-10:
      a. ``### finding <finding_id> <rule_id> <path>:<start>-<end>``
      b. newline
      c. patch_text (with trailing \\n appended if missing)
      d. trailing blank line
    """
    from autofix.cli import fix_command

    diff_body = (
        "--- a/module.py\n"
        "+++ b/module.py\n"
        "@@ -1 +1 @@\n"
        "-import os\n"
        "+import sys\n"
    )
    patch = _make_llm_patch(finding_id="fid_xyz", path="module.py", body=diff_body)

    # Force coordinator to return one LLM_PATCH task.
    task = _make_repair_task(finding_id="fid_xyz", path="module.py", rule_id="linter:mypy:type-error")
    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: [task],
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", lambda t, **kw: patch, raising=False)

    file_before = (basic_repo / "module.py").read_bytes()

    ns = argparse.Namespace(
        root=basic_repo, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 0, f"Expected rc=0 (AC-19.a), got {rc}. Stderr:\n{captured.err}"
    # Source byte-identical.
    assert (basic_repo / "module.py").read_bytes() == file_before, (
        "Suggest-only must not mutate source files"
    )
    # AC-10.a: header line uses literal "### finding " prefix and the ident tuple.
    expected_header = "### finding fid_xyz linter:mypy:type-error module.py:1-1"
    assert expected_header in captured.out, (
        f"Expected header {expected_header!r} on stdout. Got:\n{captured.out}"
    )
    # AC-10.c: full patch text appears on stdout.
    assert diff_body in captured.out, (
        f"Patch body must be present on stdout. Got:\n{captured.out}"
    )
    # AC-10.d: trailing blank line after each block. After patch_text+\n,
    # there should be at least one '\n\n' separating it from anything that
    # follows (or end-of-output). We check the canonical block ends with \n\n.
    block_idx = captured.out.find(expected_header)
    assert block_idx >= 0
    after_block = captured.out[block_idx:]
    # The block ends with patch_text (with trailing \n) then a blank line.
    assert after_block.rstrip("\n").endswith("+import sys"), (
        f"Patch body should be the last non-empty content. After:\n{after_block!r}"
    )


# ---------------------------------------------------------------------------
# AC-11 rejection one-liner format: produce_patch returns None → stdout
# writes ``### finding <ident> — rejected: <reason>`` line.
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_suggest_only_prints_rejection_line(
    basic_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-11: rejection (None return) writes one line with the cache reason."""
    from autofix.cli import fix_command
    import hashlib
    import json

    task = _make_repair_task(
        finding_id="fid_reject", path="module.py",
        rule_id="linter:mypy:return-type",
    )

    # Pre-write the cache envelope that produce_patch would have written.
    file_bytes = (basic_repo / "module.py").read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    cache_key = hashlib.sha256(("fid_reject" + file_sha + "opus").encode()).hexdigest()
    cache_dir = basic_repo / ".autofix" / "cache" / "llm_patches"
    cache_dir.mkdir(parents=True, exist_ok=True)
    envelope = {
        "version": 1,
        "key": cache_key,
        "finding_id": "fid_reject",
        "file_sha": file_sha,
        "model": "opus",
        "status": "rejected",
        "patch": None,
        "reason": "empty_diff",
        "hunk_count": 0,
        "created_at": "2026-05-07T00:00:00Z",
    }
    (cache_dir / f"{cache_key}.json").write_text(json.dumps(envelope), encoding="utf-8")

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: [task],
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", lambda t, **kw: None, raising=False)

    ns = argparse.Namespace(
        root=basic_repo, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 0
    # AC-11: line format is "### finding <id> <rule> <path>:<s>-<e> — rejected: <reason>"
    expected = "### finding fid_reject linter:mypy:return-type module.py:1-1 — rejected: empty_diff"
    assert expected in captured.out, (
        f"Expected rejection one-liner {expected!r} on stdout. Got:\n{captured.out}"
    )


@pytest.mark.integration
def test_suggest_rejection_unknown_when_envelope_missing(
    basic_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-11: missing cache envelope falls back to literal 'unknown' as reason."""
    from autofix.cli import fix_command

    task = _make_repair_task(
        finding_id="fid_no_envelope", path="module.py",
        rule_id="linter:mypy:type-error",
    )
    # Do NOT write a cache envelope.
    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: [task],
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", lambda t, **kw: None, raising=False)

    ns = argparse.Namespace(
        root=basic_repo, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 0
    expected = "### finding fid_no_envelope linter:mypy:type-error module.py:1-1 — rejected: unknown"
    assert expected in captured.out, (
        f"Expected unknown-fallback line. Got:\n{captured.out}"
    )


# ---------------------------------------------------------------------------
# AC-22.a + AC-6: --apply --suggest applies deterministic deletions AND
# emits preview block. (Apply path runs first, preview second.)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_apply_and_suggest_applies_deterministic_and_previews(
    basic_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-6: --apply --suggest applies deterministic deletions + emits preview.

    Verifies (a) source file's `import os` line is removed (deterministic
    apply happened) and (b) preview block for an LLM_PATCH-tier task appears
    on stdout.
    """
    from autofix.cli import fix_command

    diff_body = (
        "--- a/module.py\n"
        "+++ b/module.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    patch = _make_llm_patch(finding_id="fid_apply_suggest", path="module.py", body=diff_body)
    task = _make_repair_task(
        finding_id="fid_apply_suggest", path="module.py",
        rule_id="linter:mypy:something",
    )

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: [task],
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", lambda t, **kw: patch, raising=False)

    ns = argparse.Namespace(
        root=basic_repo, apply=True, force=False,
        suggest=True, auto_llm=False, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 0, f"rc={rc}. Stderr:\n{captured.err}"
    # Deterministic deletion happened: `import os\n` was the unused-import
    # finding emitted by the analyzer for our fixture.
    after = (basic_repo / "module.py").read_bytes().decode()
    assert "import os\n" not in after, (
        f"Deterministic deletion should have removed `import os\\n`. File now:\n{after}"
    )
    # Preview block for the LLM_PATCH task appeared.
    assert "### finding fid_apply_suggest" in captured.out, (
        f"Expected preview header on stdout. Got:\n{captured.out}"
    )


# ---------------------------------------------------------------------------
# AC-17 / AC-22.g: no-events invariant — events.jsonl never created/appended
# by the fix command on either suggest path.
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_suggest_does_not_write_events_jsonl(
    basic_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-17 / AC-22.g: events.jsonl is not created/appended by --suggest.

    Cache directory IS expected to be created by produce_patch; that is
    explicitly excluded from this assertion.
    """
    from autofix.cli import fix_command

    events_file = basic_repo / ".autofix" / "events.jsonl"
    pre_state = (events_file.exists(), events_file.read_bytes() if events_file.exists() else b"")

    diff_body = (
        "--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n-import os\n+import sys\n"
    )
    patch = _make_llm_patch(finding_id="fid_noevents", path="module.py", body=diff_body)
    task = _make_repair_task(finding_id="fid_noevents", path="module.py")

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: [task],
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", lambda t, **kw: patch, raising=False)

    ns = argparse.Namespace(
        root=basic_repo, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=None,
    )
    fix_command.run(ns)
    capsys.readouterr()

    post_state = (events_file.exists(), events_file.read_bytes() if events_file.exists() else b"")
    assert pre_state == post_state, (
        f"events.jsonl must not be created/appended by --suggest. "
        f"pre={pre_state}, post={post_state}"
    )


# ---------------------------------------------------------------------------
# AC-7 / AC-8 / AC-9: coordinate_repairs is called with threshold=0.6, root=None;
# produce_patch is called with the args.root as the root keyword.
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_suggest_calls_coordinator_with_pinned_kwargs(
    basic_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-7 / AC-8: coordinate_repairs called with threshold=0.6 and root=None.

    Also verifies AC-9: produce_patch is called with root=args.root.
    """
    from autofix.cli import fix_command

    coord_calls: list = []
    produce_calls: list = []

    def fake_coord(findings, *, threshold, root):
        coord_calls.append({"threshold": threshold, "root": root})
        # Return one LLM_PATCH task so produce_patch is exercised.
        return [_make_repair_task(finding_id="fid_coord", path="module.py")]

    def fake_produce(task, *, root, **kw):
        produce_calls.append({"task": task, "root": root, "kw": kw})
        return None

    monkeypatch.setattr(fix_command, "coordinate_repairs", fake_coord, raising=False)
    monkeypatch.setattr(fix_command, "produce_patch", fake_produce, raising=False)

    ns = argparse.Namespace(
        root=basic_repo, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=None,
    )
    fix_command.run(ns)
    capsys.readouterr()

    assert len(coord_calls) == 1, f"Expected exactly one coord call, got {len(coord_calls)}"
    assert coord_calls[0]["threshold"] == 0.6, (
        f"AC-8: threshold must be 0.6, got {coord_calls[0]['threshold']!r}"
    )
    assert coord_calls[0]["root"] is None, (
        f"AC-7/AC-17: root must be None to suppress UnmappedRuleTier; got {coord_calls[0]['root']!r}"
    )
    # AC-9: produce_patch called with root=args.root (the actual basic_repo path).
    assert len(produce_calls) == 1
    assert produce_calls[0]["root"] == basic_repo, (
        f"AC-9: produce_patch root must equal args.root, got {produce_calls[0]['root']!r}"
    )
    # AC-9 explicit: model kwarg is OMITTED (default takes effect).
    assert "model" not in produce_calls[0]["kw"], (
        f"AC-9: model kwarg must be omitted, got kw={produce_calls[0]['kw']!r}"
    )


# ---------------------------------------------------------------------------
# AC-7 filter: only LLM_PATCH-tier tasks are consumed; HUMAN_ONLY and
# DETERMINISTIC tasks from the coordinator output are filtered out.
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_suggest_filters_to_llm_patch_tier_only(
    basic_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-7: only RepairTier.LLM_PATCH tasks are passed to produce_patch."""
    from autofix.cli import fix_command
    from autofix.evidence.schema import CandidateFinding
    from autofix.repair import RepairTask, RepairTier

    def _t(rule_id: str, fid: str, tier: RepairTier) -> RepairTask:
        f = CandidateFinding(
            rule_id=rule_id, path="module.py",
            symbol_name="", normalized_import="",
            start_line=1, end_line=1, changed_slice="", finding_id=fid,
        )
        return RepairTask(finding=f, tier=tier, reason="rule_mapped")

    tasks = [
        _t("unused-import.intra-file", "det_1", RepairTier.DETERMINISTIC),
        _t("linter:mypy:foo", "llm_1", RepairTier.LLM_PATCH),
        _t("low-conf-rule", "human_1", RepairTier.HUMAN_ONLY),
        _t("linter:mypy:bar", "llm_2", RepairTier.LLM_PATCH),
    ]
    seen_ids: list[str] = []

    def fake_produce(task, **kw):
        seen_ids.append(task.finding.finding_id)
        return None

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: tasks,
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", fake_produce, raising=False)

    ns = argparse.Namespace(
        root=basic_repo, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=None,
    )
    fix_command.run(ns)
    capsys.readouterr()

    assert seen_ids == ["llm_1", "llm_2"], (
        f"AC-7: only LLM_PATCH tasks should reach produce_patch, got {seen_ids!r}"
    )
