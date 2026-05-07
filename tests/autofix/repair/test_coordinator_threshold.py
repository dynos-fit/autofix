"""Threshold validation, demotion semantics, and never-drop invariant tests.

Covers AC-6 (threshold validation), AC-7 (below-threshold demotion + strict
less-than boundary), and AC-12 (never-drop / preserves order).
"""
from __future__ import annotations

import math

import pytest

from autofix.evidence.schema import CandidateFinding
from autofix.repair import RepairTier, coordinate_repairs


def _finding(rule_id: str, *, analyzer_confidence: float = 1.0, fid: str | None = None) -> CandidateFinding:
    return CandidateFinding(
        rule_id,
        "pkg/mod.py",
        "my_func",
        "os",
        1,
        1,
        "",
        fid or f"fp_{rule_id}",
        analyzer_confidence=analyzer_confidence,
    )


# ---------------------------------------------------------------------------
# AC-6: Threshold validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_threshold",
    [-0.01, 1.01, float("nan"), float("inf"), float("-inf")],
)
def test_threshold_out_of_range_raises_value_error(bad_threshold: float) -> None:
    """NaN, +/-inf, and out-of-range floats must raise ``ValueError``."""
    with pytest.raises(ValueError) as excinfo:
        coordinate_repairs([], threshold=bad_threshold)
    msg = str(excinfo.value)
    assert "threshold" in msg, f"error message must mention 'threshold': {msg!r}"
    # The offending value must be visible in the message (per AC-6).
    if math.isnan(bad_threshold):
        assert "nan" in msg.lower()
    else:
        assert repr(bad_threshold) in msg or str(bad_threshold) in msg


@pytest.mark.parametrize("good_threshold", [0.0, 1.0])
def test_threshold_boundaries_accepted(good_threshold: float) -> None:
    """``threshold=0.0`` and ``threshold=1.0`` are inclusive and accepted."""
    # An empty input is fine — we only assert no ValueError is raised.
    result = coordinate_repairs([], threshold=good_threshold)
    assert result == []


# ---------------------------------------------------------------------------
# AC-7: Below-threshold demotion (strict less-than)
# ---------------------------------------------------------------------------


def test_below_threshold_demotes_even_deterministic_rule() -> None:
    """A deterministic-tier rule with confidence < threshold must be HUMAN_ONLY.

    Proves the confidence gate runs BEFORE the mapping lookup.
    """
    finding = _finding("linter:ruff:F401", analyzer_confidence=0.5)
    [task] = coordinate_repairs([finding], threshold=0.6)
    assert task.tier is RepairTier.HUMAN_ONLY
    assert task.reason == "below_threshold"


def test_confidence_equals_threshold_is_not_demoted() -> None:
    """Equality boundary: ``confidence == threshold`` is NOT below threshold.

    Strict less-than gate; equality flows through to mapping lookup and
    produces the deterministic outcome for an allowlisted rule_id.
    """
    finding = _finding("linter:ruff:F401", analyzer_confidence=0.6)
    [task] = coordinate_repairs([finding], threshold=0.6)
    assert task.tier is RepairTier.DETERMINISTIC
    assert task.reason == "rule_mapped"


# ---------------------------------------------------------------------------
# AC-12: Never-drop invariant (length, order, identity)
# ---------------------------------------------------------------------------


def test_never_drops_preserves_count_order_and_identity() -> None:
    """N mixed inputs -> exactly N tasks; output order matches input order;
    each task's ``finding`` is the same object as the corresponding input.
    """
    inputs = [
        _finding("linter:ruff:F401", fid="fp_a"),                       # rule_mapped
        _finding("linter:mypy:misc", fid="fp_b"),                       # prefix_mapped
        _finding("custom:internal:weird", fid="fp_c"),                  # no_mapping
        _finding("linter:ruff:F401", analyzer_confidence=0.1, fid="fp_d"),  # below_threshold
        _finding("unused-import.intra-file", fid="fp_e"),               # rule_mapped
    ]
    tasks = coordinate_repairs(inputs, threshold=0.5)

    assert len(tasks) == len(inputs), (
        f"never-drop: expected {len(inputs)} tasks, got {len(tasks)}"
    )
    for index, (task, original) in enumerate(zip(tasks, inputs)):
        assert task.finding is original, (
            f"index {index}: finding identity must be preserved (got rule_id "
            f"{task.finding.rule_id!r}, expected {original.rule_id!r})"
        )

    # Spot-check the per-row reasons to defend against accidental reordering.
    assert [t.reason for t in tasks] == [
        "rule_mapped",
        "prefix_mapped",
        "no_mapping",
        "below_threshold",
        "rule_mapped",
    ]


def test_never_drops_with_generator_input() -> None:
    """Generator inputs are consumed once and produce one task per element."""
    inputs = [
        _finding("linter:ruff:F401", fid="g0"),
        _finding("custom:weird:rule", fid="g1"),
        _finding("llm:code-quality:foo", fid="g2"),
    ]
    tasks = coordinate_repairs((f for f in inputs), threshold=0.5)
    assert len(tasks) == 3
    assert [t.finding.finding_id for t in tasks] == ["g0", "g1", "g2"]
