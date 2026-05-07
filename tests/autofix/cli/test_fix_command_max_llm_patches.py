"""TDD tests for ``autofix fix --max-llm-patches`` (AC-20 / AC-22.h).

Pinned ACs:
- AC-20: --max-llm-patches positive-int flag, default None (no cap)
- AC-20.a: counter increments only on cache MISS produce calls
- AC-20.b: when counter == cap, subsequent tasks are skipped (no produce, no preview, no apply)
- AC-20.c: structured event LLMPatchBudgetExceeded with payload {limit, attempted, remaining_skipped}
- AC-20.d: stderr summary line ``LLM patch budget exceeded: limit=<L>, attempted=<A>, skipped=<S>``
- AC-20.e: skipped tasks are not failures (rc=0 per AC-19.a/AC-19.c)
- AC-20.f: absent flag → no counter, no event, no summary line
- AC-20.g: non-positive value → usage error before detect/run_scan

These tests are RED — production code does not implement the cap yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
        cwd=root, check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-04-17T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-04-17T00:00:00Z",
        },
    )


def _make_llm_patch(*, finding_id: str, path: str, body: str = "stub-diff"):
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
def cap_repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    (tmp_path / "module.py").write_text("import os\n\nx = 1\n", encoding="utf-8")
    _commit(tmp_path, "init")
    return tmp_path


# ---------------------------------------------------------------------------
# AC-22.h primary: --max-llm-patches=2 with 3 LLM_PATCH tasks → produce called
# 2x; LLMPatchBudgetExceeded event emitted; stderr summary printed; rc=0.
# (Preview path)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_max_llm_patches_caps_produce_calls_in_suggest_mode(
    cap_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-20.b / AC-20.d / AC-22.h: cap=2, 3 tasks → only 2 produce calls."""
    from autofix.cli import fix_command

    tasks = [
        _make_repair_task(finding_id="fid_1", path="module.py"),
        _make_repair_task(finding_id="fid_2", path="module.py"),
        _make_repair_task(finding_id="fid_3", path="module.py"),
    ]
    produce_calls: list = []

    def fake_produce(task, **kw):
        produce_calls.append(task.finding.finding_id)
        return _make_llm_patch(finding_id=task.finding.finding_id, path="module.py")

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: tasks,
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", fake_produce, raising=False)

    ns = argparse.Namespace(
        root=cap_repo, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=2,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 0, f"AC-19.a / AC-20.e: rc=0 on cap-skip. Got rc={rc}. Stderr:\n{captured.err}"
    assert produce_calls == ["fid_1", "fid_2"], (
        f"AC-20.b: produce called exactly twice (cap), got {produce_calls!r}"
    )
    # AC-20.d: stderr summary line, exactly once.
    expected = "LLM patch budget exceeded: limit=2, attempted=2, skipped=1"
    assert expected in captured.err, (
        f"AC-20.d: stderr summary {expected!r} expected. Got:\n{captured.err}"
    )
    assert captured.err.count(expected) == 1, (
        f"AC-20.d: summary line written exactly once, got {captured.err.count(expected)}"
    )


# ---------------------------------------------------------------------------
# AC-20.c: LLMPatchBudgetExceeded event with the documented payload shape.
# We capture events emitted to the in-memory bus by patching the
# event-emitter callable on fix_command (whatever name it uses).
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_max_llm_patches_emits_budget_exceeded_event(
    cap_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-20.c: emit one LLMPatchBudgetExceeded event with payload {limit, attempted, remaining_skipped}."""
    from autofix.cli import fix_command

    tasks = [
        _make_repair_task(finding_id="fid_1", path="module.py"),
        _make_repair_task(finding_id="fid_2", path="module.py"),
        _make_repair_task(finding_id="fid_3", path="module.py"),
    ]

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: tasks,
        raising=False,
    )
    monkeypatch.setattr(
        fix_command, "produce_patch",
        lambda task, **kw: _make_llm_patch(
            finding_id=task.finding.finding_id, path="module.py",
        ),
        raising=False,
    )

    # AC-20.c says the event flows through the in-memory event-bus contract
    # owned by autofix.events. The fix command must invoke an emitter
    # symbol that we can intercept. We try to patch a likely emitter name
    # on fix_command; if not present, we accept that the test will fail
    # the existence assertion below.
    captured_events: list[tuple[str, dict]] = []

    def fake_emit(event_name: str, payload: dict, /, **kw) -> None:
        captured_events.append((event_name, dict(payload)))

    # Several possible emitter names — patch whichever is bound on the
    # fix_command module. We use raising=False to tolerate either choice.
    for name in ("emit_event", "publish_event", "_emit_event", "_publish_event"):
        if hasattr(fix_command, name):
            monkeypatch.setattr(fix_command, name, fake_emit, raising=False)

    ns = argparse.Namespace(
        root=cap_repo, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=2,
    )
    fix_command.run(ns)
    capsys.readouterr()

    budget_events = [(n, p) for (n, p) in captured_events if n == "LLMPatchBudgetExceeded"]
    assert len(budget_events) == 1, (
        f"AC-20.c: exactly one LLMPatchBudgetExceeded event expected. "
        f"All captured events: {captured_events!r}"
    )
    payload = budget_events[0][1]
    assert payload.get("limit") == 2, f"AC-20.c: limit=2 expected, got {payload!r}"
    assert payload.get("attempted") == 2, f"AC-20.c: attempted=2 expected, got {payload!r}"
    assert payload.get("remaining_skipped") == 1, (
        f"AC-20.c: remaining_skipped=1 expected, got {payload!r}"
    )


# ---------------------------------------------------------------------------
# AC-22.h cap-on-apply: --apply --auto-llm --max-llm-patches=2 with 3 tasks
# also caps at 2 produce calls.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_max_llm_patches_caps_in_auto_llm_mode(
    cap_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-20 symmetry: same counter/cap applies to --apply --auto-llm path."""
    from autofix.cli import fix_command

    tasks = [
        _make_repair_task(finding_id=f"fid_{i}", path="module.py")
        for i in range(3)
    ]
    produce_calls: list = []

    def fake_produce(task, **kw):
        produce_calls.append(task.finding.finding_id)
        # Return None so we don't have to construct a real-applying diff.
        return None

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: tasks,
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", fake_produce, raising=False)

    ns = argparse.Namespace(
        root=cap_repo, apply=True, force=False,
        suggest=False, auto_llm=True, max_llm_patches=2,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 0, f"AC-19.c / AC-20.e: rc=0 on cap-skip. Got rc={rc}. Stderr:\n{captured.err}"
    assert len(produce_calls) == 2, (
        f"AC-20.b: cap=2 must limit produce_patch to 2 calls in --auto-llm path. "
        f"Got {len(produce_calls)} calls: {produce_calls!r}"
    )
    expected = "LLM patch budget exceeded: limit=2, attempted=2, skipped=1"
    assert expected in captured.err, (
        f"AC-20.d: summary line expected. Stderr:\n{captured.err}"
    )


# ---------------------------------------------------------------------------
# AC-20.a: cache HITs do NOT count toward the cap. Pre-write 2 cache
# envelopes and pass --max-llm-patches=1; even though produce_patch is
# called 3 times (returns the cached LLMPatch), the cap should not fire.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_max_llm_patches_does_not_count_cache_hits(
    cap_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-20.a: cache HITs do not increment the cap counter."""
    from autofix.cli import fix_command

    # Tasks: fid_a (cached), fid_b (cached), fid_c (cache miss).
    tasks = [
        _make_repair_task(finding_id="fid_a", path="module.py"),
        _make_repair_task(finding_id="fid_b", path="module.py"),
        _make_repair_task(finding_id="fid_c", path="module.py"),
    ]

    file_bytes = (cap_repo / "module.py").read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    cache_dir = cap_repo / ".autofix" / "cache" / "llm_patches"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for fid in ("fid_a", "fid_b"):
        key = hashlib.sha256((fid + file_sha + "opus").encode()).hexdigest()
        envelope = {
            "version": 1, "key": key, "finding_id": fid,
            "file_sha": file_sha, "model": "opus",
            "status": "ok",
            "patch": "stub-diff", "reason": None,
            "hunk_count": 1, "created_at": "2026-05-07T00:00:00Z",
        }
        (cache_dir / f"{key}.json").write_text(json.dumps(envelope), encoding="utf-8")

    # Stub produce_patch: returns LLMPatch with cache_hit=True for fid_a,
    # fid_b (so even though the fix command calls produce_patch, the result
    # claims cache_hit=True). For fid_c, return cache_hit=False.
    from autofix.repair import LLMPatch
    def fake_produce(task, **kw):
        fid = task.finding.finding_id
        return LLMPatch(
            finding_id=fid, file_path="module.py", patch_text="stub-diff",
            model="opus", cache_hit=(fid in ("fid_a", "fid_b")), hunk_count=1,
        )

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: tasks,
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", fake_produce, raising=False)

    ns = argparse.Namespace(
        root=cap_repo, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=1,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 0
    # AC-20.a: only the cache MISS (fid_c) increments the counter, so the
    # cap of 1 is NOT exceeded — no summary line.
    assert "LLM patch budget exceeded" not in captured.err, (
        f"AC-20.a: cache HITs must not count toward the cap. "
        f"Stderr should NOT contain budget-exceeded summary. Got:\n{captured.err}"
    )


# ---------------------------------------------------------------------------
# AC-20.f: default (no --max-llm-patches) → no cap, all patches attempted,
# no summary line, no event.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_max_llm_patches_default_no_cap(
    cap_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-20.f: default None → no counter, no cap, no event, no summary."""
    from autofix.cli import fix_command

    tasks = [
        _make_repair_task(finding_id=f"fid_{i}", path="module.py") for i in range(5)
    ]
    produce_calls: list = []

    def fake_produce(task, **kw):
        produce_calls.append(task.finding.finding_id)
        return None

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: tasks,
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", fake_produce, raising=False)

    ns = argparse.Namespace(
        root=cap_repo, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=None,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc == 0
    assert len(produce_calls) == 5, (
        f"AC-20.f: with no cap, all 5 tasks attempted. Got {produce_calls!r}"
    )
    assert "LLM patch budget exceeded" not in captured.err, (
        f"AC-20.f: no summary line when flag absent. Stderr:\n{captured.err}"
    )


# ---------------------------------------------------------------------------
# AC-20.g: --max-llm-patches=0 and --max-llm-patches=-1 rejected at parse
# time (non-zero exit, usage error stderr, BEFORE detect/run_scan).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_value", [0, -1, -42])
@pytest.mark.integration
def test_max_llm_patches_rejects_non_positive(
    cap_repo: Path, capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch, bad_value: int,
) -> None:
    """AC-20.g: --max-llm-patches <= 0 → usage error, no detect/run_scan."""
    from autofix.cli import fix_command

    detect_calls: list = []

    def fake_detect(*a, **kw):
        detect_calls.append((a, kw))
        return ([], 1.0)

    monkeypatch.setattr(fix_command, "detect", fake_detect, raising=False)

    ns = argparse.Namespace(
        root=cap_repo, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=bad_value,
    )
    rc = fix_command.run(ns)
    captured = capsys.readouterr()

    assert rc != 0, (
        f"AC-20.g: max_llm_patches={bad_value} must produce non-zero rc. Got rc={rc}"
    )
    assert detect_calls == [], (
        f"AC-20.g: detect() must not run before usage validation. "
        f"detect_calls={detect_calls!r}"
    )
    assert "--max-llm-patches" in captured.err, (
        f"AC-20.g: stderr must name the flag. Got:\n{captured.err}"
    )


# ---------------------------------------------------------------------------
# AC-20.b safety: skipped tasks are not previewed and not applied.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(30)
def test_max_llm_patches_skipped_tasks_not_previewed(
    cap_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-20.b: tasks beyond the cap are not previewed (no header on stdout)."""
    from autofix.cli import fix_command

    tasks = [
        _make_repair_task(finding_id="fid_kept", path="module.py"),
        _make_repair_task(finding_id="fid_skipped", path="module.py"),
    ]

    def fake_produce(task, **kw):
        return _make_llm_patch(
            finding_id=task.finding.finding_id, path="module.py",
            body="--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n-import os\n+import sys\n",
        )

    monkeypatch.setattr(
        fix_command, "coordinate_repairs",
        lambda findings, *, threshold, root: tasks,
        raising=False,
    )
    monkeypatch.setattr(fix_command, "produce_patch", fake_produce, raising=False)

    ns = argparse.Namespace(
        root=cap_repo, apply=False, force=False,
        suggest=True, auto_llm=False, max_llm_patches=1,
    )
    fix_command.run(ns)
    captured = capsys.readouterr()

    assert "### finding fid_kept" in captured.out, (
        "First task should be previewed before cap fires"
    )
    assert "### finding fid_skipped" not in captured.out, (
        "AC-20.b: skipped task must NOT be previewed"
    )
