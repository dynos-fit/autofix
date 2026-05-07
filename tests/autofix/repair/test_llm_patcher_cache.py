"""Cache-behavior tests for ``autofix.repair.llm_patcher.produce_patch`` (AC-22.c).

Three sub-cases:

  1. **Cache hit** — pre-seed a valid success envelope; ``produce_patch`` returns
     the cached artifact with ``cache_hit=True``; the LLM seam is NOT invoked
     (a counter wrapper fails the test if it is); zero new telemetry envelopes
     appear in events.jsonl (per AC-7's "do not emit on cache hit").

  2. **Identity-mismatch silent miss** — parameterized over the three
     equality-checks AC-9 lists that can be tampered with under the same
     cache_key (key, file_sha, model — ``finding_id`` is part of the key
     derivation so it cannot be seeded to the same path with a wrong
     finding_id). The poisoned envelope is silently treated as a miss; the
     scheduler IS called; no ``LLMPatchRejected`` event fires for the
     mismatch (silent miss per AC-9); and the freshly-written envelope
     overwrites the poisoned one with the correct identity fields.

The cache-hit assertion patterns mirror
``tests/autofix/analyzers/llm_judgment/test_base_cache_hit.py`` and
``test_base_cache_miss.py`` (sibling LLM-judgment cache pattern).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import autofix.llm.scheduler as _scheduler_mod
from autofix.evidence.schema import CandidateFinding
from autofix.repair.coordinator import RepairTask, RepairTier


_VALID_DIFF_BODY = (
    "--- a/target.py\n"
    "+++ b/target.py\n"
    "@@ -1 +1 @@\n"
    '-print("a")\n'
    '+print("b")\n'
)
_VALID_LLM_RESPONSE = f"<<<DIFF>>>\n{_VALID_DIFF_BODY}<<<END_DIFF>>>\n"


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def real_git_repo(tmp_path: Path) -> Path:
    _git(["init", "-q", "-b", "main"], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=tmp_path)
    (tmp_path / "target.py").write_text('print("a")\n', encoding="utf-8")
    _git(["add", "target.py"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "seed"], cwd=tmp_path)
    return tmp_path


def _make_task(finding_id: str = "fp_cache_001") -> RepairTask:
    finding = CandidateFinding(
        "linter:ruff:E501",
        "target.py",
        "main",
        "",
        1,
        1,
        "fix the print",
        finding_id,
    )
    return RepairTask(finding=finding, tier=RepairTier.LLM_PATCH, reason="prefix_mapped")


def _compute_cache_key(finding_id: str, file_bytes: bytes, model: str = "opus") -> str:
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    return hashlib.sha256(
        (finding_id + file_sha + model).encode("utf-8")
    ).hexdigest()


def _read_events(root: Path) -> list[dict]:
    path = root / ".autofix" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _seed_envelope(cache_path: Path, envelope: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")


# --------------------------------------------------------------------------
# Sub-case 1: Cache hit returns the cached artifact, scheduler not called.
# --------------------------------------------------------------------------


def test_cache_hit_returns_cached_artifact_without_invoking_scheduler(
    real_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-seeded success envelope is returned with ``cache_hit=True``; LLM not called."""
    from autofix.repair.llm_patcher import LLMPatch, produce_patch

    task = _make_task(finding_id="fp_cache_hit_001")
    file_bytes = (real_git_repo / "target.py").read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    cache_key = _compute_cache_key("fp_cache_hit_001", file_bytes)
    cache_path = (
        real_git_repo / ".autofix" / "cache" / "llm_patches" / f"{cache_key}.json"
    )

    seeded_patch_text = "--- a/target.py\n+++ b/target.py\n@@ -1 +1 @@\n-x\n+y\n"
    envelope = {
        "version": 1,
        "key": cache_key,
        "finding_id": "fp_cache_hit_001",
        "file_sha": file_sha,
        "model": "opus",
        "created_at": "2026-05-07T00:00:00Z",
        "status": "ok",
        "patch": seeded_patch_text,
        "reason": None,
        "hunk_count": 7,
    }
    _seed_envelope(cache_path, envelope)

    invoke_calls = {"count": 0}

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        invoke_calls["count"] += 1
        raise AssertionError(
            "Scheduler.invoke_judgment must NOT be called on a cache hit"
        )

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    result = produce_patch(task, root=real_git_repo)

    assert result is not None
    assert isinstance(result, LLMPatch)
    assert result.cache_hit is True
    # The artifact mirrors the seeded envelope's content fields (AC-7).
    assert result.patch_text == seeded_patch_text
    assert result.hunk_count == 7
    assert result.finding_id == "fp_cache_hit_001"
    assert result.file_path == "target.py"
    assert result.model == "opus"

    assert invoke_calls["count"] == 0


def test_cache_hit_emits_no_telemetry(
    real_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache hit suppresses all three new telemetry envelopes (AC-7)."""
    from autofix.repair.llm_patcher import produce_patch

    task = _make_task(finding_id="fp_cache_hit_no_tele")
    file_bytes = (real_git_repo / "target.py").read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    cache_key = _compute_cache_key("fp_cache_hit_no_tele", file_bytes)
    cache_path = (
        real_git_repo / ".autofix" / "cache" / "llm_patches" / f"{cache_key}.json"
    )

    envelope = {
        "version": 1,
        "key": cache_key,
        "finding_id": "fp_cache_hit_no_tele",
        "file_sha": file_sha,
        "model": "opus",
        "created_at": "2026-05-07T00:00:00Z",
        "status": "ok",
        "patch": "--- a/target.py\n+++ b/target.py\n@@ -1 +1 @@\n-x\n+y\n",
        "reason": None,
        "hunk_count": 1,
    }
    _seed_envelope(cache_path, envelope)

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        raise AssertionError("LLM must not be called on cache hit")

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    produce_patch(task, root=real_git_repo)

    events = _read_events(real_git_repo)
    new_event_names = {"LLMPatchInvoked", "LLMPatchProduced", "LLMPatchRejected"}
    relevant = [e for e in events if e["event"] in new_event_names]
    assert relevant == [], (
        f"cache hit must emit zero new telemetry envelopes, got {[e['event'] for e in relevant]}"
    )


def test_cache_hit_rejected_envelope_returns_none_without_telemetry(
    real_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-seeded rejection envelope returns ``None`` and emits no telemetry (AC-7).

    The ``status="rejected"`` cached envelope must short-circuit the patcher
    without re-invoking the LLM and without re-emitting telemetry.
    """
    from autofix.repair.llm_patcher import produce_patch

    task = _make_task(finding_id="fp_cache_hit_rejected")
    file_bytes = (real_git_repo / "target.py").read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    cache_key = _compute_cache_key("fp_cache_hit_rejected", file_bytes)
    cache_path = (
        real_git_repo / ".autofix" / "cache" / "llm_patches" / f"{cache_key}.json"
    )

    envelope = {
        "version": 1,
        "key": cache_key,
        "finding_id": "fp_cache_hit_rejected",
        "file_sha": file_sha,
        "model": "opus",
        "created_at": "2026-05-07T00:00:00Z",
        "status": "rejected",
        "patch": None,
        "reason": "did_not_apply",
        "hunk_count": None,
    }
    _seed_envelope(cache_path, envelope)

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        raise AssertionError("LLM must not be called on cache hit (rejected)")

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    result = produce_patch(task, root=real_git_repo)
    assert result is None

    events = _read_events(real_git_repo)
    new_event_names = {"LLMPatchInvoked", "LLMPatchProduced", "LLMPatchRejected"}
    relevant = [e for e in events if e["event"] in new_event_names]
    assert relevant == []


# --------------------------------------------------------------------------
# Sub-case 2: Identity-mismatch silently treated as miss (AC-9 sec-001 defense).
# Per the plan: parameterized over key, file_sha, model. Not finding_id —
# finding_id is part of the cache_key derivation so a poisoned envelope under
# the same path with a different finding_id cannot exist by construction.
# --------------------------------------------------------------------------


def _poison_envelope(field: str) -> dict:
    """Return an envelope dict where exactly one identity field is wrong."""
    base = {
        "version": 1,
        "key": "GOOD",
        "finding_id": "GOOD",
        "file_sha": "GOOD",
        "model": "GOOD",
        "created_at": "2026-05-07T00:00:00Z",
        "status": "ok",
        "patch": "POISONED PATCH BODY -- MUST NOT BE RETURNED",
        "reason": None,
        "hunk_count": 9999,
    }
    base[field] = "POISONED"
    return base


_MISMATCH_FIELDS = ["key", "file_sha", "model"]


@pytest.mark.parametrize("poisoned_field", _MISMATCH_FIELDS)
def test_identity_mismatch_treated_as_miss_invokes_llm(
    real_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    poisoned_field: str,
) -> None:
    """A poisoned envelope (one identity field wrong) is silently a miss; LLM IS called."""
    from autofix.repair.llm_patcher import produce_patch

    finding_id = f"fp_poisoned_{poisoned_field}"
    task = _make_task(finding_id=finding_id)
    file_bytes = (real_git_repo / "target.py").read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    cache_key = _compute_cache_key(finding_id, file_bytes)
    cache_path = (
        real_git_repo / ".autofix" / "cache" / "llm_patches" / f"{cache_key}.json"
    )

    envelope = _poison_envelope(poisoned_field)
    # Fill the other (non-poisoned) identity fields with the correct values so
    # ONLY ``poisoned_field`` causes the mismatch.
    correct = {
        "key": cache_key,
        "finding_id": finding_id,
        "file_sha": file_sha,
        "model": "opus",
    }
    for k, v in correct.items():
        if k != poisoned_field:
            envelope[k] = v
    _seed_envelope(cache_path, envelope)

    invoke_calls = {"count": 0}

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        invoke_calls["count"] += 1
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    result = produce_patch(task, root=real_git_repo)

    # Mismatch -> miss -> LLM called -> fresh artifact returned.
    assert invoke_calls["count"] == 1, (
        f"poisoned {poisoned_field}: scheduler MUST be invoked (miss), got "
        f"{invoke_calls['count']} calls"
    )
    assert result is not None
    assert result.cache_hit is False, (
        f"poisoned {poisoned_field}: result must be cache_miss, not honored as hit"
    )
    # Importantly: the patcher did NOT return the poisoned patch text.
    assert result.patch_text != "POISONED PATCH BODY -- MUST NOT BE RETURNED"
    assert result.hunk_count != 9999


@pytest.mark.parametrize("poisoned_field", _MISMATCH_FIELDS)
def test_identity_mismatch_silent_no_rejection_event(
    real_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    poisoned_field: str,
) -> None:
    """The mismatch is silent: no ``LLMPatchRejected`` envelope is emitted for it (AC-9).

    The ``LLMPatchInvoked`` envelope IS emitted (proving the patcher proceeded
    past the cache lookup). On a successful follow-on validation, only
    ``LLMPatchInvoked`` + ``LLMPatchProduced`` should appear — NOT a rejection.
    """
    from autofix.repair.llm_patcher import produce_patch

    finding_id = f"fp_silent_{poisoned_field}"
    task = _make_task(finding_id=finding_id)
    file_bytes = (real_git_repo / "target.py").read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    cache_key = _compute_cache_key(finding_id, file_bytes)
    cache_path = (
        real_git_repo / ".autofix" / "cache" / "llm_patches" / f"{cache_key}.json"
    )

    envelope = _poison_envelope(poisoned_field)
    correct = {
        "key": cache_key,
        "finding_id": finding_id,
        "file_sha": file_sha,
        "model": "opus",
    }
    for k, v in correct.items():
        if k != poisoned_field:
            envelope[k] = v
    _seed_envelope(cache_path, envelope)

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    produce_patch(task, root=real_git_repo)

    events = _read_events(real_git_repo)
    relevant = [
        e for e in events
        if e["event"] in {"LLMPatchInvoked", "LLMPatchProduced", "LLMPatchRejected"}
    ]
    # Invoked emitted (proving miss), Produced emitted (canned diff applies),
    # NO Rejected event because the silent-miss defense (AC-9) does not log.
    event_names = [e["event"] for e in relevant]
    assert "LLMPatchInvoked" in event_names
    assert "LLMPatchRejected" not in event_names, (
        f"silent-miss must NOT emit LLMPatchRejected for {poisoned_field} mismatch; "
        f"got events {event_names}"
    )


@pytest.mark.parametrize("poisoned_field", _MISMATCH_FIELDS)
def test_identity_mismatch_overwrites_poisoned_envelope(
    real_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    poisoned_field: str,
) -> None:
    """The freshly-written envelope replaces the poisoned one with correct identity fields."""
    from autofix.repair.llm_patcher import produce_patch

    finding_id = f"fp_overwrite_{poisoned_field}"
    task = _make_task(finding_id=finding_id)
    file_bytes = (real_git_repo / "target.py").read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    cache_key = _compute_cache_key(finding_id, file_bytes)
    cache_path = (
        real_git_repo / ".autofix" / "cache" / "llm_patches" / f"{cache_key}.json"
    )

    envelope = _poison_envelope(poisoned_field)
    correct = {
        "key": cache_key,
        "finding_id": finding_id,
        "file_sha": file_sha,
        "model": "opus",
    }
    for k, v in correct.items():
        if k != poisoned_field:
            envelope[k] = v
    _seed_envelope(cache_path, envelope)

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    produce_patch(task, root=real_git_repo)

    # The envelope on disk after the call must have all four identity fields
    # correct AND must NOT contain the poisoned patch body.
    overwritten = json.loads(cache_path.read_text(encoding="utf-8"))
    assert overwritten["key"] == cache_key
    assert overwritten["finding_id"] == finding_id
    assert overwritten["file_sha"] == file_sha
    assert overwritten["model"] == "opus"
    assert overwritten["status"] == "ok"
    assert overwritten["patch"] != "POISONED PATCH BODY -- MUST NOT BE RETURNED"
    assert overwritten["hunk_count"] != 9999
