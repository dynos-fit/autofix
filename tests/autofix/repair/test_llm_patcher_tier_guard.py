"""Tier-guard tests for ``autofix.repair.llm_patcher.produce_patch`` (AC-22.d).

Per AC-4: ``produce_patch`` raises ``ValueError`` (with a message that names
the actual ``task.tier`` value) when ``task.tier is not RepairTier.LLM_PATCH``.
The two non-LLM_PATCH enum members are ``DETERMINISTIC`` and ``HUMAN_ONLY``;
both must trigger the guard. The ``LLM_PATCH`` tier must NOT raise the guard
(smoke check — even if downstream steps fail, the guard itself does not fire).

The ``ValueError`` message MUST contain a substring mention of the offending
tier value so a debugger / log reader can identify the wrong tier without
guessing. We match against the enum's ``.value`` string (per ``RepairTier``,
``"deterministic"`` and ``"human-only"``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autofix.evidence.schema import CandidateFinding
from autofix.repair.coordinator import RepairTask, RepairTier


def _finding() -> CandidateFinding:
    return CandidateFinding(
        "linter:ruff:E501",
        "irrelevant.py",
        "main",
        "",
        1,
        1,
        "anything",
        "fp_tier_guard",
    )


def test_deterministic_tier_raises_value_error_with_tier_name(tmp_path: Path) -> None:
    """``RepairTier.DETERMINISTIC`` triggers the guard; message names the tier."""
    from autofix.repair.llm_patcher import produce_patch

    task = RepairTask(
        finding=_finding(), tier=RepairTier.DETERMINISTIC, reason="rule_mapped"
    )

    with pytest.raises(ValueError) as exc_info:
        produce_patch(task, root=tmp_path)

    msg = str(exc_info.value)
    assert "deterministic" in msg.lower(), (
        f"ValueError message must mention the offending tier value 'deterministic'; got: {msg!r}"
    )


def test_human_only_tier_raises_value_error_with_tier_name(tmp_path: Path) -> None:
    """``RepairTier.HUMAN_ONLY`` triggers the guard; message names the tier."""
    from autofix.repair.llm_patcher import produce_patch

    task = RepairTask(
        finding=_finding(), tier=RepairTier.HUMAN_ONLY, reason="no_mapping"
    )

    with pytest.raises(ValueError) as exc_info:
        produce_patch(task, root=tmp_path)

    msg = str(exc_info.value)
    assert "human-only" in msg.lower(), (
        f"ValueError message must mention the offending tier value 'human-only'; got: {msg!r}"
    )


def test_llm_patch_tier_does_not_raise_value_error_from_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``RepairTier.LLM_PATCH`` does NOT trigger the tier guard (smoke).

    Downstream behavior (file missing -> ``did_not_apply`` rejection) is
    exercised here only to prove the guard itself does not fire. The function
    returns ``None`` (rejection sentinel) rather than raising ``ValueError``.
    """
    import autofix.llm.scheduler as _scheduler_mod
    from autofix.repair.llm_patcher import produce_patch

    task = RepairTask(
        finding=_finding(), tier=RepairTier.LLM_PATCH, reason="prefix_mapped"
    )

    # Seam stubbed in case the patcher reaches it.
    def fake_invoke(self, prompt: str, *, model: str) -> str:
        return ""

    monkeypatch.setattr(_scheduler_mod.Scheduler, "invoke_judgment", fake_invoke)

    # No file at root/irrelevant.py -> AC-5 OSError path -> returns None,
    # NOT a ValueError. The point of this test is that the guard does not fire.
    result = produce_patch(task, root=tmp_path)
    assert result is None  # not LLMPatch, but importantly NOT a raised ValueError
