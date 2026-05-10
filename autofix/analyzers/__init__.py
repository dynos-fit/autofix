"""Analyzers subpackage.

Public surface: ``analyze_files`` (the entry point) and
``CandidateFinding`` (the return-type dataclass).
"""
from autofix.analyzers._registry import analyze_files
from autofix.evidence.schema import CandidateFinding

__all__ = ["analyze_files", "CandidateFinding"]
