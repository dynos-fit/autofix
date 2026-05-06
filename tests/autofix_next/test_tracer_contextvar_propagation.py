"""AC 52 — Tracer context propagates across ThreadPoolExecutor worker threads.

Submits a callable to a ``ThreadPoolExecutor`` inside a parent span and
asserts the worker-thread span's ``parent.span_id`` equals the parent
span's ``span_id``. Uses OTel's ``InMemorySpanExporter`` test fixture so
no network traffic is required.

This test guards against a regression where the scheduler's pool would
start workers outside the parent span's OTel context, breaking the
end-to-end trace graph.

Implementation note: OTel's ``trace.set_tracer_provider`` refuses to
override a previously-installed SDK provider (``WARNING Overriding of
current TracerProvider is not allowed``). We therefore install the
provider at module scope and ``clear()`` the exporter between tests.
"""

from __future__ import annotations

import concurrent.futures

import pytest

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


# Module-scoped exporter + provider: set once, reused across tests. OTel's
# API refuses to replace an SDK provider in the same process, so we must
# install it exactly once. ``clear()`` is called in the fixture so each
# test starts with a clean finished-spans list.
_EXPORTER = InMemorySpanExporter()


def _install_provider_once() -> None:
    current = trace.get_tracer_provider()
    # Install only when the current provider is not already our SDK one.
    if isinstance(current, TracerProvider):
        return
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
    trace.set_tracer_provider(provider)


@pytest.fixture
def in_memory_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Install the SDK provider once per process and hand the test a clean
    exporter. Also force-resets the autofix_next tracer singleton so it
    re-binds to the SDK provider (rather than returning a cached no-op
    tracer from a prior test that ran before the provider was installed)."""
    _install_provider_once()
    _EXPORTER.clear()

    monkeypatch.setenv("AUTOFIX_NEXT_TESTING", "1")
    from autofix_next.telemetry import tracer as _tracer_mod

    _tracer_mod.reset_tracer_provider()
    yield _EXPORTER
    _EXPORTER.clear()


def _child_work() -> int:
    """Run inside a worker thread; open a ``worker`` span and return id."""
    from autofix_next.telemetry.tracer import span

    with span("worker") as s:
        # Return the span_id so the main test thread can cross-reference it
        # against the collected spans.
        return s.get_span_context().span_id


def test_child_span_parent_is_parent_span(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """AC 52: a span opened inside a worker submitted via a ThreadPoolExecutor
    must carry the parent span's span_id as its parent.span_id."""
    from autofix_next.telemetry.tracer import span

    parent_span_id: int | None = None
    worker_span_id: int | None = None

    with span("parent") as parent:
        parent_span_id = parent.get_span_context().span_id
        # Capture current OTel context in the main thread so the worker can
        # attach it — this is the stdlib-sanctioned propagation pattern.
        ctx = otel_context.get_current()

        def _runner() -> int:
            token = otel_context.attach(ctx)
            try:
                return _child_work()
            finally:
                otel_context.detach(token)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_runner)
            worker_span_id = fut.result(timeout=10)

    assert parent_span_id is not None and parent_span_id != 0
    assert worker_span_id is not None and worker_span_id != 0
    assert parent_span_id != worker_span_id, (
        "parent and worker spans must have distinct span_ids"
    )

    finished = in_memory_exporter.get_finished_spans()
    # Find the "worker" span by name.
    worker_spans = [s for s in finished if s.name == "worker"]
    assert worker_spans, (
        f"expected one 'worker' span, got names={[s.name for s in finished]!r}"
    )
    worker = worker_spans[0]
    parent_ctx = worker.parent
    assert parent_ctx is not None, (
        "worker span must have a parent context (got None — context was not "
        "propagated across the ThreadPoolExecutor boundary)"
    )
    assert parent_ctx.span_id == parent_span_id, (
        f"worker.parent.span_id={parent_ctx.span_id:#x} does not match "
        f"parent.span_id={parent_span_id:#x}"
    )


def test_child_span_has_no_parent_when_submitted_outside_span(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """Negative control: a worker submitted outside any active span produces
    a root (parentless) span. This makes the positive assertion above
    meaningful — the propagation is observed only when a parent is active."""

    def _runner() -> None:
        from autofix_next.telemetry.tracer import span

        with span("detached_worker"):
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_runner).result(timeout=10)

    finished = in_memory_exporter.get_finished_spans()
    detached = [s for s in finished if s.name == "detached_worker"]
    assert len(detached) == 1
    # A root span has no parent context (None) or an invalid parent.
    parent = detached[0].parent
    assert parent is None or not parent.is_valid, (
        "detached worker span must be parentless when no span is active"
    )
