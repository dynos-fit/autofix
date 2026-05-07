"""Passthrough adapters for external linters (ruff, mypy, pylint, etc.)."""

from __future__ import annotations

# Re-export linter submodules for convenient importing
from autofix.analyzers.linter_passthrough import mypy as mypy
from autofix.analyzers.linter_passthrough import ruff as ruff

__all__ = ["mypy", "ruff"]
