"""Analyzers subpackage.

Public surface: ``analyze_files`` (entry point), ``CandidateFinding``
(return-type dataclass), ``ANALYZER_REGISTRY`` (read-only view of the
analyzer-name -> callable mapping), and ``reset_passthrough_state``
(per-scan cleanup hook).
"""
from autofix.analyzers._registry import (
    ANALYZER_REGISTRY,
    analyze_files,
    reset_passthrough_state,
)
from autofix.evidence.schema import CandidateFinding

__all__ = [
    "analyze_files",
    "CandidateFinding",
    "ANALYZER_REGISTRY",
    "reset_passthrough_state",
]
