"""Repair coordinator package for the autofix repair subsystem.

This package exposes the public surface of the repair routing layer:
- ``RepairTier``: three-member enum classifying how a finding should be repaired.
- ``RepairTask``: frozen dataclass pairing a finding with its assigned tier.
- ``coordinate_repairs``: pure routing function that maps a list of
  ``CandidateFinding`` objects to a list of ``RepairTask`` objects.

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

__all__ = ["RepairTier", "RepairTask", "coordinate_repairs"]
