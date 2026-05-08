"""Dispatch table tests for ``autofix.repair.coordinator.coordinate_repairs``.

Covers AC-8/AC-9: rule_id -> (tier, reason) mapping. The exact-match check
must run before the prefix check; unmapped rule_ids produce ``no_mapping``.
Also asserts the public re-exports surfaced by ``autofix.repair``.
"""
from __future__ import annotations

import pytest

from autofix.evidence.schema import CandidateFinding
from autofix.repair import RepairTask, RepairTier, coordinate_repairs


def _finding(rule_id: str, *, analyzer_confidence: float = 1.0) -> CandidateFinding:
    return CandidateFinding(
        rule_id,
        "pkg/mod.py",
        "my_func",
        "os",
        1,
        1,
        "",
        f"fp_{rule_id}",
        analyzer_confidence=analyzer_confidence,
    )


# (rule_id, expected_tier, expected_reason)
_DISPATCH_ROWS: list[tuple[str, RepairTier, str]] = [
    # Exact-match wins over prefix match.
    ("unused-import.intra-file", RepairTier.DETERMINISTIC, "rule_mapped"),
    ("linter:ruff:F401", RepairTier.DETERMINISTIC, "rule_mapped"),
    # Prefix-only matches: ruff code outside the allowlist, mypy, llm.
    ("linter:ruff:E999", RepairTier.LLM_PATCH, "prefix_mapped"),
    ("linter:mypy:misc", RepairTier.LLM_PATCH, "prefix_mapped"),
    ("llm:code-quality:magic-number", RepairTier.LLM_PATCH, "prefix_mapped"),
    # ARCH-013/016 follow-up: every LLM-judgment family now routes to
    # LLM_PATCH so produce_patch can attempt a fix. Previously these
    # all fell to HUMAN_ONLY ("no_mapping"), bypassing the LLM patcher
    # — the entire LLM-judgment family was effectively dead code on
    # the auto-fix path.
    ("llm:dead-code:unused-import", RepairTier.LLM_PATCH, "prefix_mapped"),
    ("llm:security:secret-leak", RepairTier.LLM_PATCH, "prefix_mapped"),
    ("llm:performance:n-plus-one", RepairTier.LLM_PATCH, "prefix_mapped"),
    # Fully unmapped -> human-only, reason no_mapping.
    ("unknown-rule-xyz", RepairTier.HUMAN_ONLY, "no_mapping"),
]


@pytest.mark.parametrize(("rule_id", "expected_tier", "expected_reason"), _DISPATCH_ROWS)
def test_dispatch_row(rule_id: str, expected_tier: RepairTier, expected_reason: str) -> None:
    """Each row in the dispatch table maps to the documented (tier, reason)."""
    finding = _finding(rule_id)
    [task] = coordinate_repairs([finding], threshold=0.5)
    assert task.tier is expected_tier
    assert task.reason == expected_reason
    # Sanity: the produced task references the original finding object.
    assert task.finding is finding


def test_exact_match_beats_prefix_for_allowlisted_ruff_code() -> None:
    """``linter:ruff:F401`` must produce ``rule_mapped`` not ``prefix_mapped``.

    Reversing the check order would silently emit the wrong reason string.
    """
    [task] = coordinate_repairs([_finding("linter:ruff:F401")], threshold=0.5)
    assert task.tier is RepairTier.DETERMINISTIC
    assert task.reason == "rule_mapped"


def test_public_exports_resolvable_from_package() -> None:
    """``from autofix.repair import RepairTier, RepairTask, coordinate_repairs`` works.

    Failure here would mean the package's ``__init__.py`` does not re-export
    the public surface.
    """
    # Smoke: each symbol is non-None and callable/usable.
    assert RepairTier.DETERMINISTIC == "deterministic"
    assert RepairTask.__name__ == "RepairTask"
    assert callable(coordinate_repairs)
