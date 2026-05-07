"""Repair coordinator package for the autofix repair subsystem.

This package exposes the public surface of the repair routing layer:
- ``RepairTier``: three-member enum classifying how a finding should be repaired.
- ``RepairTask``: frozen dataclass pairing a finding with its assigned tier.
- ``coordinate_repairs``: pure routing function that maps a list of
  ``CandidateFinding`` objects to a list of ``RepairTask`` objects.
- ``LLMPatch``: validated unified-diff patch artifact produced by the LLM patcher.
- ``produce_patch``: produce-only LLM patcher that returns an ``LLMPatch`` or
  ``None``, without mutating user source files (Phase 3c leg).

The coordinator does NOT mutate source files and does NOT produce patches.
Its sole responsibility is tier assignment and optional telemetry emission.
Downstream Phase 3b (deterministic patcher) and Phase 3c (LLM-patch pipeline)
consume the tasks list produced here.
"""

from autofix.repair.coordinator import (
    RepairTask,
    RepairTier,
    coordinate_repairs,
)
from autofix.repair.llm_patcher import LLMPatch, produce_patch

__all__ = ["RepairTier", "RepairTask", "coordinate_repairs", "LLMPatch", "produce_patch"]
