"""Shape tests for ``RepairTask`` (frozen+slots+field-order) and ``RepairTier``.

Covers AC-3 (RepairTier exact members + str-mixin) and AC-4 (RepairTask
frozen+slotted with exact field order).
"""
from __future__ import annotations

import dataclasses

from autofix.repair import RepairTask, RepairTier


# ---------------------------------------------------------------------------
# RepairTask: frozen + slotted + field order (AC-4)
# ---------------------------------------------------------------------------


def test_repair_task_is_frozen_dataclass() -> None:
    """``@dataclass(frozen=True)`` is asserted via ``__dataclass_params__``."""
    assert RepairTask.__dataclass_params__.frozen is True


def test_repair_task_uses_slots() -> None:
    """``@dataclass(slots=True)`` materializes ``__slots__`` on the class dict."""
    # Either the dunder is in __dict__ (slots=True) or the attribute exists.
    assert "__slots__" in RepairTask.__dict__ or hasattr(RepairTask, "__slots__")


def test_repair_task_field_order_is_finding_tier_reason() -> None:
    """Field declaration order is locked: ``finding`` then ``tier`` then ``reason``."""
    names = [f.name for f in dataclasses.fields(RepairTask)]
    assert names == ["finding", "tier", "reason"]


# ---------------------------------------------------------------------------
# RepairTier: exact three members + str-mixin (AC-3)
# ---------------------------------------------------------------------------


def test_repair_tier_has_exactly_three_members_with_exact_values() -> None:
    """Membership and string values are pinned by spec."""
    members = {m.name: m.value for m in RepairTier}
    assert members == {
        "DETERMINISTIC": "deterministic",
        "LLM_PATCH": "llm-patch",
        "HUMAN_ONLY": "human-only",
    }


def test_repair_tier_is_str_mixin() -> None:
    """``RepairTier`` mixes ``str`` so members compare equal to their values.

    This is load-bearing for downstream JSON serialization.
    """
    assert isinstance(RepairTier.DETERMINISTIC, str)
    assert RepairTier.DETERMINISTIC == "deterministic"
    assert RepairTier.LLM_PATCH == "llm-patch"
    assert RepairTier.HUMAN_ONLY == "human-only"
