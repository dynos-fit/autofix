"""Passthrough adapters for external linters (ruff, pylint, etc.)."""

from __future__ import annotations

# Re-export ruff submodule for convenient importing
from autofix.analyzers.linter_passthrough import ruff as ruff

__all__ = ["ruff"]
