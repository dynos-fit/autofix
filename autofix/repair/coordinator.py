"""Repair coordinator: routes ``CandidateFinding`` objects to repair tiers.

## Reason strings (closed vocabulary)

Every ``RepairTask`` carries exactly one of these four ``reason`` strings:

- ``"rule_mapped"``   — finding's rule_id is in ``_DETERMINISTIC_RULES`` and
                        its confidence is >= threshold.  Tier: DETERMINISTIC.
- ``"prefix_mapped"`` — finding's rule_id starts with a prefix in
                        ``_LLM_PATCH_PREFIXES`` and confidence is >= threshold.
                        Tier: LLM_PATCH.
- ``"below_threshold"`` — finding's analyzer_confidence is strictly less than
                          threshold.  Tier: HUMAN_ONLY regardless of rule_id.
                          The threshold gate is checked FIRST, before any
                          mapping lookup.
- ``"no_mapping"``    — confidence >= threshold but rule_id is neither in
                        ``_DETERMINISTIC_RULES`` nor starts with any prefix in
                        ``_LLM_PATCH_PREFIXES``.  Tier: HUMAN_ONLY.

## Tier semantics

- DETERMINISTIC — findings where a fully mechanical rewrite exists (e.g. ruff
  auto-fix codes).  Phase 3b patchers handle these without LLM involvement.
- LLM_PATCH     — findings that require a model-generated diff suggestion.
  Phase 3c pipeline handles these.
- HUMAN_ONLY    — findings that need human judgment: either confidence too low
  or no automated repair strategy is mapped.

## Deterministic allowlist (``_DETERMINISTIC_RULES``)

Exact rule_id strings whose tier is unconditionally DETERMINISTIC (subject to
the confidence gate).  13 entries covering unused-import and common ruff fixes:

    "unused-import.intra-file", "linter:ruff:F401", "linter:ruff:F811",
    "linter:ruff:F841", "linter:ruff:I001", "linter:ruff:COM812",
    "linter:ruff:Q000", "linter:ruff:UP006", "linter:ruff:UP007",
    "linter:ruff:UP032", "linter:ruff:E501", "linter:ruff:W291",
    "linter:ruff:W293"

## LLM-patch prefix list (``_LLM_PATCH_PREFIXES``)

Three prefix strings used as a fallback after the exact-match check fails.
Any rule_id whose string starts with one of these prefixes is routed to
LLM_PATCH:

    "linter:ruff:", "linter:mypy:", "llm:code-quality:"

The exact-match check always runs before the prefix check, so e.g.
``"linter:ruff:F401"`` produces ``reason="rule_mapped"`` (not
``"prefix_mapped"``), even though ``"linter:ruff:"`` would also match.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from autofix.evidence.schema import CandidateFinding


class RepairTier(str, Enum):
    """Three-member enum classifying how a finding should be repaired."""

    DETERMINISTIC = "deterministic"
    LLM_PATCH = "llm-patch"
    HUMAN_ONLY = "human-only"


@dataclass(slots=True, frozen=True)
class RepairTask:
    """Immutable pairing of a CandidateFinding with its assigned repair tier.

    Fields (in declaration order):
        finding: the original CandidateFinding (held by reference, not copied).
        tier:    one of the three RepairTier values.
        reason:  one of the four closed-vocabulary strings:
                 "rule_mapped", "prefix_mapped", "below_threshold", "no_mapping".
    """

    finding: CandidateFinding
    tier: RepairTier
    reason: str


# ---------------------------------------------------------------------------
# Mapping tables (module-level, immutable)
# ---------------------------------------------------------------------------

_DETERMINISTIC_RULES: frozenset[str] = frozenset(
    {
        "unused-import.intra-file",
        "linter:ruff:F401",
        "linter:ruff:F811",
        "linter:ruff:F841",
        "linter:ruff:I001",
        "linter:ruff:COM812",
        "linter:ruff:Q000",
        "linter:ruff:UP006",
        "linter:ruff:UP007",
        "linter:ruff:UP032",
        "linter:ruff:E501",
        "linter:ruff:W291",
        "linter:ruff:W293",
    }
)

_LLM_PATCH_PREFIXES: tuple[str, ...] = (
    "linter:ruff:",
    "linter:mypy:",
    "llm:code-quality:",
    # ARCH-013/016 follow-up: the LLM-judgment family (security,
    # dead-code, performance) shipped in ARCH-013 was not yet routable
    # to ``LLM_PATCH`` — every finding from those analyzers fell to
    # ``HUMAN_ONLY`` ("no_mapping"), bypassing the LLM patcher
    # entirely. Surfaced when the crawl dogfood found 7 ``llm:security``
    # findings on ``agent_loop.py`` and 0 patches were generated.
    "llm:dead-code:",
    "llm:security:",
    "llm:performance:",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def coordinate_repairs(
    findings: Iterable[CandidateFinding],
    *,
    threshold: float,
    root: Path | None = None,
) -> list[RepairTask]:
    """Route each finding to a repair tier.

    Args:
        findings:  Iterable of CandidateFinding objects.  The iterable is
                   consumed once; passing a generator is safe.
        threshold: Confidence gate in [0.0, 1.0].  Findings whose
                   ``analyzer_confidence`` is strictly less than this value are
                   demoted to HUMAN_ONLY regardless of rule_id.  Must be a
                   finite float in [0.0, 1.0]; NaN, inf, and out-of-range
                   values raise ValueError.
        root:      Optional repository root.  When not None, the coordinator
                   appends one ``UnmappedRuleTier`` telemetry envelope per
                   unique unmapped rule_id to ``<root>/.autofix/events.jsonl``.
                   When None (default), the function is fully pure: no
                   filesystem IO is performed.

    Returns:
        A list of RepairTask objects in input order.  Every input finding
        produces exactly one output task; no findings are dropped or
        deduplicated.

    Raises:
        ValueError: if threshold is NaN, infinite, or outside [0.0, 1.0].
    """
    # -- AC-6: Threshold validation BEFORE iterating findings ----------------
    if math.isnan(threshold) or not (0.0 <= threshold <= 1.0):
        raise ValueError(
            f"threshold must be a finite float in [0.0, 1.0], got {threshold!r}"
        )

    tasks: list[RepairTask] = []
    # Track unmapped rule_ids seen in this call for deduplication (AC-10).
    unmapped_seen: set[str] = set()

    for finding in findings:
        rule_id = finding.rule_id

        # -- AC-7: Confidence gate runs FIRST ---------------------------------
        if finding.analyzer_confidence < threshold:
            tasks.append(RepairTask(finding=finding, tier=RepairTier.HUMAN_ONLY, reason="below_threshold"))
            continue

        # -- AC-8: Exact-match BEFORE prefix-match ----------------------------
        if rule_id in _DETERMINISTIC_RULES:
            tasks.append(RepairTask(finding=finding, tier=RepairTier.DETERMINISTIC, reason="rule_mapped"))
            continue

        if rule_id.startswith(_LLM_PATCH_PREFIXES):
            tasks.append(RepairTask(finding=finding, tier=RepairTier.LLM_PATCH, reason="prefix_mapped"))
            continue

        # -- AC-9: Unmapped ---------------------------------------------------
        tasks.append(RepairTask(finding=finding, tier=RepairTier.HUMAN_ONLY, reason="no_mapping"))
        unmapped_seen.add(rule_id)

    # -- AC-10: Telemetry (only when root is provided, only for no_mapping) ---
    if root is not None and unmapped_seen:
        events_dir = root / ".autofix"
        events_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
        events_file = events_dir / "events.jsonl"
        ts = datetime.now(timezone.utc).isoformat()
        with events_file.open("a") as fh:
            for rule_id in unmapped_seen:
                envelope = {"event": "UnmappedRuleTier", "rule_id": rule_id, "ts": ts}
                fh.write(json.dumps(envelope) + "\n")

    return tasks
