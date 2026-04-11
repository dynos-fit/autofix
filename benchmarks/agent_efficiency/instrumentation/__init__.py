"""Decorator and runtime patch utilities for non-invasive benchmarking."""

from .core import apply_patch_specs, load_patch_specs, read_trace_events, traced

__all__ = [
    "apply_patch_specs",
    "load_patch_specs",
    "read_trace_events",
    "traced",
]
