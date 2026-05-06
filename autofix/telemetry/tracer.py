"""OpenTelemetry tracer lifecycle for autofix (AC 1-5, 61)."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Span, Tracer

_LOCK = threading.Lock()
_TRACER: Tracer | None = None
_PROVIDER_INSTALLED: bool = False


def get_tracer() -> Tracer:
    """Return the cached module-level Tracer, installing the provider on first call."""
    global _TRACER, _PROVIDER_INSTALLED
    if _TRACER is not None:
        return _TRACER
    with _LOCK:
        if _TRACER is not None:
            return _TRACER
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        if endpoint:
            try:
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                provider = TracerProvider()
                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
                )
                trace.set_tracer_provider(provider)
                _PROVIDER_INSTALLED = True
            except ImportError as exc:
                # Route to the events.jsonl pipeline rather than stderr so
                # the failure is observable in structured telemetry. Lazy
                # import keeps tracer.py free of a hard dependency on
                # events_log at import time (avoids cycles — events_log
                # does not import tracer, but we keep this symmetrical).
                try:
                    from autofix.telemetry import events_log

                    events_log.append_event(
                        Path.cwd(),
                        "TracerProviderConfigurationFailed",
                        {"reason": str(exc)},
                    )
                except OSError:
                    pass
        # When endpoint is unset OR OTLP import fails, the default no-op provider
        # stays active (opentelemetry-api ships NoOpTracerProvider as the default).
        _TRACER = trace.get_tracer("autofix")
        return _TRACER


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Span]:
    """Open a new span as the current one; usable as ``with span(...):``."""
    tracer = get_tracer()
    # Filter None attrs (OTel rejects None attribute values in some SDKs).
    clean_attrs = {k: v for k, v in attrs.items() if v is not None}
    with tracer.start_as_current_span(name, attributes=clean_attrs) as s:
        yield s


def reset_tracer_provider() -> None:
    """Test-only: clear the cached tracer. Requires AUTOFIX_TESTING=1."""
    if os.environ.get("AUTOFIX_TESTING") != "1":
        raise RuntimeError(
            "reset_tracer_provider() requires AUTOFIX_TESTING=1 env var; "
            "production code must not call it."
        )
    global _TRACER, _PROVIDER_INSTALLED
    with _LOCK:
        _TRACER = None
        _PROVIDER_INSTALLED = False


__all__ = ["get_tracer", "span", "reset_tracer_provider"]
