"""Optional benchmark instrumentation helpers.

The production code should not require ``agent_bench`` to be installed for
normal scans and fixes. This module exposes the same decorator surface but
falls back to no-op wrappers when the benchmark package is unavailable.
"""

from __future__ import annotations

from typing import Any, Callable

try:
    from agent_bench import trace_llm as benchmark_trace_llm
    from agent_bench import trace_tool as benchmark_trace_tool
except ImportError:
    def _identity_decorator(_func: Callable[..., Any] | None = None, **_: Any):
        def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
            return func

        if _func is not None and callable(_func):
            return _func
        return decorate

    benchmark_trace_llm = _identity_decorator
    benchmark_trace_tool = _identity_decorator


__all__ = ["benchmark_trace_llm", "benchmark_trace_tool"]
