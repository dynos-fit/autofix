"""Happy-path tests for ``autofix.repair.llm_patcher.produce_patch`` (AC-22.a).

A real ``git init``'d temp working tree seeds a file at the path captured by
the upstream task's finding path; the LLM seam is monkeypatched at the
class-method level on ``autofix.llm.scheduler.Scheduler.invoke_judgment`` to
return a canned valid ``<<<DIFF>>>...<<<END_DIFF>>>`` fenced unified diff that
applies cleanly to the seeded file.

Pinned ACs (and indirectly verified):
- AC-1, AC-2, AC-3 (public surface / dataclass shape / signature)
- AC-6, AC-7, AC-8 (cache key / cache lookup before LLM / success envelope shape)
- AC-14a, AC-14d (fence extraction / hunk count derivation)
- AC-15 (real ``git apply --check`` passes)
- AC-17 (telemetry order: ``LLMPatchInvoked`` precedes ``LLMPatchProduced``)
- AC-19 (the two emitted envelopes use the canonical event names)
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


# Canned LLM response: a fenced unified diff that converts ``print("a")\n`` to
# ``print("b")\n``. Applies cleanly under ``git apply --check`` against the
# fixture-seeded file.
_VALID_DIFF_BODY = (
    "--- a/target.py\n"
    "+++ b/target.py\n"
    "@@ -1 +1 @@\n"
    '-print("a")\n'
    '+print("b")\n'
)
_VALID_LLM_RESPONSE = f"<<<DIFF>>>\n{_VALID_DIFF_BODY}<<<END_DIFF>>>\n"


def _git(args: list[str], cwd: Path) -> None:
    """Run a git command in ``cwd``. Used to set up the test working tree."""
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def real_git_repo(tmp_path: Path) -> Path:
    """``git init`` a tmp dir, seed ``target.py``, commit it.

    The commit is required so ``git apply --check`` has a base to apply against
    (Risk Notes — "test flakiness from real git apply").
    """
    _git(["init", "-q", "-b", "main"], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=tmp_path)

    (tmp_path / "target.py").write_text('print("a")\n', encoding="utf-8")
    _git(["add", "target.py"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "seed"], cwd=tmp_path)
    return tmp_path


def _make_task(rule_id: str = "linter:ruff:E501") -> RepairTask:
    finding = CandidateFinding(
        rule_id,
        "target.py",
        "main",
        "",
        1,
        1,
        "fix the print",
        "fp_happy_path_001",
    )
    return RepairTask(finding=finding, tier=RepairTier.LLM_PATCH, reason="prefix_mapped")


def _read_events(root: Path) -> list[dict]:
    """Read events.jsonl as a list of parsed envelope dicts (in order)."""
    path = root / ".autofix" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_happy_path_returns_artifact_with_correct_fields(
    real_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``produce_patch`` returns ``LLMPatch`` with all six fields populated correctly."""
    from autofix.repair.llm_patcher import LLMPatch, produce_patch

    task = _make_task()

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    result = produce_patch(task, root=real_git_repo)

    assert result is not None, "happy-path must produce an artifact, not None"
    assert isinstance(result, LLMPatch)
    assert result.finding_id == "fp_happy_path_001"
    assert result.file_path == "target.py"
    assert result.model == "opus"  # AC-3 default
    assert result.cache_hit is False
    assert result.hunk_count == 1
    # Patch text equals the stripped fence body (AC-14a).
    assert result.patch_text.strip() == _VALID_DIFF_BODY.strip()


def test_happy_path_writes_success_cache_envelope(
    real_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A success cache envelope is written at the AC-6 path with AC-8 success shape."""
    from autofix.repair.llm_patcher import produce_patch

    task = _make_task()

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    produce_patch(task, root=real_git_repo)

    # Compute the deterministic cache_key (AC-6).
    file_bytes = (real_git_repo / "target.py").read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    cache_key = hashlib.sha256(
        ("fp_happy_path_001" + file_sha + "opus").encode("utf-8")
    ).hexdigest()

    cache_path = (
        real_git_repo / ".autofix" / "cache" / "llm_patches" / f"{cache_key}.json"
    )
    assert cache_path.exists(), f"cache envelope must exist at {cache_path}"

    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    # AC-8 success-envelope shape.
    assert envelope["version"] == 1
    assert envelope["key"] == cache_key
    assert envelope["finding_id"] == "fp_happy_path_001"
    assert envelope["file_sha"] == file_sha
    assert envelope["model"] == "opus"
    assert envelope["status"] == "ok"
    assert envelope["patch"] is not None
    assert envelope["patch"].strip() == _VALID_DIFF_BODY.strip()
    assert envelope["reason"] is None
    assert envelope["hunk_count"] == 1
    # ISO-8601 UTC with trailing Z.
    assert "created_at" in envelope and envelope["created_at"].endswith("Z")


def test_happy_path_emits_invoked_then_produced_in_order(
    real_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LLMPatchInvoked`` appears in events.jsonl before ``LLMPatchProduced``.

    Per AC-17 the invocation envelope MUST be persisted before the LLM call so
    that a crash mid-call still leaves a trace.
    """
    from autofix.repair.llm_patcher import produce_patch

    task = _make_task()

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    produce_patch(task, root=real_git_repo)

    events = _read_events(real_git_repo)
    relevant = [
        e for e in events
        if e["event"] in {"LLMPatchInvoked", "LLMPatchProduced", "LLMPatchRejected"}
    ]
    assert [e["event"] for e in relevant] == ["LLMPatchInvoked", "LLMPatchProduced"], (
        f"expected exactly two envelopes in order, got: {[e['event'] for e in relevant]}"
    )

    # No rejection envelope appeared.
    assert all(e["event"] != "LLMPatchRejected" for e in events)


def test_happy_path_invoked_payload_shape(
    real_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LLMPatchInvoked`` payload contains ``finding_id``, ``file_sha``, ``model``."""
    from autofix.repair.llm_patcher import produce_patch

    task = _make_task()

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    produce_patch(task, root=real_git_repo)

    file_bytes = (real_git_repo / "target.py").read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()

    events = _read_events(real_git_repo)
    invoked = [e for e in events if e["event"] == "LLMPatchInvoked"]
    assert len(invoked) == 1
    payload = invoked[0]["scan_event"]
    assert payload["finding_id"] == "fp_happy_path_001"
    assert payload["file_sha"] == file_sha
    assert payload["model"] == "opus"


def test_happy_path_produced_payload_shape(
    real_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LLMPatchProduced`` payload contains ``finding_id``, ``file_sha``, ``hunk_count``."""
    from autofix.repair.llm_patcher import produce_patch

    task = _make_task()

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    produce_patch(task, root=real_git_repo)

    file_bytes = (real_git_repo / "target.py").read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()

    events = _read_events(real_git_repo)
    produced = [e for e in events if e["event"] == "LLMPatchProduced"]
    assert len(produced) == 1
    payload = produced[0]["scan_event"]
    assert payload["finding_id"] == "fp_happy_path_001"
    assert payload["file_sha"] == file_sha
    assert payload["hunk_count"] == 1


def test_happy_path_does_not_modify_seeded_file(
    real_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Produce-only contract: ``produce_patch`` must NOT write to user source."""
    from autofix.repair.llm_patcher import produce_patch

    task = _make_task()
    original_bytes = (real_git_repo / "target.py").read_bytes()

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    produce_patch(task, root=real_git_repo)

    # The seeded file must be unchanged on disk.
    assert (real_git_repo / "target.py").read_bytes() == original_bytes


def test_happy_path_public_exports_resolvable(
    real_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LLMPatch`` and ``produce_patch`` are importable via the package re-export.

    Pins AC-1 (only two exports) and AC-20 (package init re-exports them).
    """
    from autofix.repair import LLMPatch, produce_patch

    assert LLMPatch.__name__ == "LLMPatch"
    assert callable(produce_patch)


def test_happy_path_module_all_exports_only_two_symbols() -> None:
    """``llm_patcher.__all__`` is exactly ``["LLMPatch", "produce_patch"]`` (AC-1)."""
    import autofix.repair.llm_patcher as patcher_mod

    assert hasattr(patcher_mod, "__all__")
    assert list(patcher_mod.__all__) == ["LLMPatch", "produce_patch"]


def test_happy_path_dataclass_field_order_is_load_bearing() -> None:
    """``LLMPatch`` field declaration order must match AC-2 exactly."""
    import dataclasses

    from autofix.repair.llm_patcher import LLMPatch

    field_names = tuple(f.name for f in dataclasses.fields(LLMPatch))
    assert field_names == (
        "finding_id",
        "file_path",
        "patch_text",
        "model",
        "cache_hit",
        "hunk_count",
    )
    # AC-2 also requires slots=True and frozen=True.
    assert LLMPatch.__dataclass_params__.frozen is True
    assert LLMPatch.__dataclass_params__.slots is True
