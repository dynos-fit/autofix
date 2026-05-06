"""Telemetry subpackage for autofix_next.

Contains the SARIF 2.1.0 emitter and the envelope-compatible events.jsonl
writer that coexists with the legacy ``autofix/runtime/dynos.py:log_event``
rows in the same file. Also exposes the OpenTelemetry tracer lifecycle
(:mod:`autofix_next.telemetry.tracer`) and per-scan correlation ID
contextvars (:mod:`autofix_next.telemetry.correlation`).
"""

from . import atomic, correlation, tracer

__all__ = ["atomic", "correlation", "tracer"]
