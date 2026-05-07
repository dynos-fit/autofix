"""Rejection-path tests for ``autofix.repair.llm_patcher.produce_patch`` (AC-22.b).

One independently-failable test per reason in the closed AC-13 vocabulary, in
the AC-13 evaluation order:

  1. ``non_diff``       — no fence, or empty fence body, or zero ``+++ b/...`` headers.
  2. ``empty_diff``     — fence body has zero ``@@`` markers.
  3. ``multi_file``     — two or more ``+++ b/...`` headers.
  4. ``path_mismatch``  — single ``+++ b/<path>`` whose path != ``finding.path``.
  5. ``did_not_apply``  — structurally valid diff that ``git apply --check``
                          rejects (hunk header points past EOF).

Each test asserts:
  - ``produce_patch`` returns ``None`` (AC-13).
  - A ``status="rejected"`` envelope at the AC-6 path with the AC-13 reason.
  - exactly one ``LLMPatchInvoked`` followed by exactly one ``LLMPatchRejected``
    envelope in events.jsonl, in that AC-17 order.
  - No ``LLMPatchProduced`` envelope appears.

Order-evaluation regression note: flipping any pair of order-dependent checks
(fence vs. hunk-count, multi-file vs. path-match) silently mis-classifies real
LLM output. These tests keep the order locked.
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


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def real_git_repo(tmp_path: Path) -> Path:
    """Same fixture shape as the happy-path file: real git repo with a seeded file."""
    _git(["init", "-q", "-b", "main"], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=tmp_path)
    (tmp_path / "target.py").write_text('print("a")\n', encoding="utf-8")
    _git(["add", "target.py"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "seed"], cwd=tmp_path)
    return tmp_path


def _make_task(finding_id: str = "fp_reject_001") -> RepairTask:
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


def _read_events(root: Path) -> list[dict]:
    path = root / ".autofix" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _expected_cache_path(root: Path, finding_id: str, model: str = "opus") -> Path:
    """Compute the AC-6 cache path for the seeded ``target.py`` content."""
    file_bytes = (root / "target.py").read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    cache_key = hashlib.sha256(
        (finding_id + file_sha + model).encode("utf-8")
    ).hexdigest()
    return root / ".autofix" / "cache" / "llm_patches" / f"{cache_key}.json"


# Canned LLM responses, one per AC-13 reason.

# AC-13 #1 ``non_diff``: fenced region is empty after stripping. AC-14a.
_RESPONSE_NON_DIFF = "<<<DIFF>>>\n\n<<<END_DIFF>>>\n"

# AC-13 #2 ``empty_diff``: fence has a ``+++`` header but zero ``@@`` markers.
# Per AC-14: fence-empty check (a) passes, then hunk-count check (b) fires.
_RESPONSE_EMPTY_DIFF = (
    "<<<DIFF>>>\n"
    "--- a/target.py\n"
    "+++ b/target.py\n"
    "<<<END_DIFF>>>\n"
)

# AC-13 #3 ``multi_file``: two ``+++ b/...`` headers in the body.
_RESPONSE_MULTI_FILE = (
    "<<<DIFF>>>\n"
    "--- a/target.py\n"
    "+++ b/target.py\n"
    "@@ -1 +1 @@\n"
    '-print("a")\n'
    '+print("b")\n'
    "--- a/other.py\n"
    "+++ b/other.py\n"
    "@@ -1 +1 @@\n"
    "-x = 1\n"
    "+x = 2\n"
    "<<<END_DIFF>>>\n"
)

# AC-13 #4 ``path_mismatch``: single ``+++ b/<path>`` whose path != ``target.py``.
_RESPONSE_PATH_MISMATCH = (
    "<<<DIFF>>>\n"
    "--- a/wrong.py\n"
    "+++ b/wrong.py\n"
    "@@ -1 +1 @@\n"
    '-print("a")\n'
    '+print("b")\n'
    "<<<END_DIFF>>>\n"
)

# AC-13 #5 ``did_not_apply``: structurally valid single-file diff whose hunk
# header line numbers point past the end of the seeded one-line file.
# ``git apply --check`` returns non-zero for this.
_RESPONSE_DID_NOT_APPLY = (
    "<<<DIFF>>>\n"
    "--- a/target.py\n"
    "+++ b/target.py\n"
    "@@ -50,1 +50,1 @@\n"
    "-nonexistent line at line 50\n"
    "+still nonexistent\n"
    "<<<END_DIFF>>>\n"
)


_REJECTION_CASES = [
    pytest.param(_RESPONSE_NON_DIFF, "non_diff", id="non_diff"),
    pytest.param(_RESPONSE_EMPTY_DIFF, "empty_diff", id="empty_diff"),
    pytest.param(_RESPONSE_MULTI_FILE, "multi_file", id="multi_file"),
    pytest.param(_RESPONSE_PATH_MISMATCH, "path_mismatch", id="path_mismatch"),
    pytest.param(_RESPONSE_DID_NOT_APPLY, "did_not_apply", id="did_not_apply"),
]


@pytest.mark.parametrize(("llm_response", "expected_reason"), _REJECTION_CASES)
def test_rejection_returns_none(
    real_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_response: str,
    expected_reason: str,
) -> None:
    """Each rejection reason returns ``None`` from ``produce_patch``."""
    from autofix.repair.llm_patcher import produce_patch

    task = _make_task(finding_id=f"fp_reject_{expected_reason}")

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return llm_response

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    result = produce_patch(task, root=real_git_repo)
    assert result is None, f"reason {expected_reason!r} must return None"


@pytest.mark.parametrize(("llm_response", "expected_reason"), _REJECTION_CASES)
def test_rejection_writes_rejected_envelope(
    real_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_response: str,
    expected_reason: str,
) -> None:
    """A ``status="rejected"`` envelope with matching reason is written to disk."""
    from autofix.repair.llm_patcher import produce_patch

    task = _make_task(finding_id=f"fp_reject_{expected_reason}")

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return llm_response

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    produce_patch(task, root=real_git_repo)

    cache_path = _expected_cache_path(real_git_repo, f"fp_reject_{expected_reason}")
    assert cache_path.exists(), f"rejection envelope must exist for {expected_reason}"

    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    # AC-8 rejection-envelope shape.
    assert envelope["version"] == 1
    assert envelope["status"] == "rejected"
    assert envelope["reason"] == expected_reason, (
        f"reason mismatch: expected {expected_reason!r}, got {envelope['reason']!r}"
    )
    assert envelope["patch"] is None
    assert envelope["hunk_count"] is None
    # Identity fields still present (AC-8 envelope shape).
    assert envelope["finding_id"] == f"fp_reject_{expected_reason}"
    assert envelope["model"] == "opus"


@pytest.mark.parametrize(("llm_response", "expected_reason"), _REJECTION_CASES)
def test_rejection_telemetry_order_and_count(
    real_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_response: str,
    expected_reason: str,
) -> None:
    """Exactly one ``LLMPatchInvoked`` then exactly one ``LLMPatchRejected``.

    No ``LLMPatchProduced`` envelope is emitted on a rejection. The order is
    ``Invoked`` first, then ``Rejected`` (AC-17).
    """
    from autofix.repair.llm_patcher import produce_patch

    task = _make_task(finding_id=f"fp_reject_{expected_reason}")

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return llm_response

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    produce_patch(task, root=real_git_repo)

    events = _read_events(real_git_repo)
    relevant = [
        e for e in events
        if e["event"] in {"LLMPatchInvoked", "LLMPatchProduced", "LLMPatchRejected"}
    ]
    assert [e["event"] for e in relevant] == ["LLMPatchInvoked", "LLMPatchRejected"], (
        f"reason {expected_reason!r}: expected Invoked->Rejected, got {[e['event'] for e in relevant]}"
    )

    # Rejection payload carries the AC-13 reason (AC-17).
    rejected = relevant[1]["scan_event"]
    assert rejected["reason"] == expected_reason
    assert rejected["finding_id"] == f"fp_reject_{expected_reason}"
    assert rejected["model"] == "opus"


def test_did_not_apply_uses_real_git_apply_check(
    real_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``did_not_apply`` case actually goes through the real subprocess.

    A regression that no-ops ``git apply --check`` (e.g. always returns 0)
    would cause this test to wrongly classify the response as success. We
    explicitly assert the rejection happens, anchoring the AC-15 contract.
    """
    from autofix.repair.llm_patcher import produce_patch

    task = _make_task(finding_id="fp_reject_did_not_apply_real_git")

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return _RESPONSE_DID_NOT_APPLY

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    result = produce_patch(task, root=real_git_repo)
    assert result is None

    events = _read_events(real_git_repo)
    rejection_envs = [e for e in events if e["event"] == "LLMPatchRejected"]
    assert len(rejection_envs) == 1
    assert rejection_envs[0]["scan_event"]["reason"] == "did_not_apply"


def test_missing_file_emits_did_not_apply_without_invoking_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: missing file -> ``did_not_apply`` rejection; no LLM call, no Invoked.

    The OSError from ``read_bytes`` is collapsed into the closed
    five-reason vocabulary as ``did_not_apply`` (rejection vocabulary stays
    closed at five). No ``LLMPatchInvoked`` envelope is emitted because no
    LLM call occurs.
    """
    # No git init, no seeded file -> read_bytes raises OSError.
    from autofix.repair.llm_patcher import produce_patch

    task = _make_task(finding_id="fp_reject_missing_file")

    invoke_calls = {"count": 0}

    def fake_invoke(self, prompt: str, *, model: str) -> str:
        invoke_calls["count"] += 1
        return ""

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    result = produce_patch(task, root=tmp_path)
    assert result is None
    assert invoke_calls["count"] == 0, "no LLM call when the file is missing"

    events = _read_events(tmp_path)
    relevant = [
        e for e in events
        if e["event"] in {"LLMPatchInvoked", "LLMPatchProduced", "LLMPatchRejected"}
    ]
    # Only the rejection envelope, with reason did_not_apply. No Invoked.
    assert [e["event"] for e in relevant] == ["LLMPatchRejected"]
    assert relevant[0]["scan_event"]["reason"] == "did_not_apply"
