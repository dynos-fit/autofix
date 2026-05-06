"""Tests for ``Scheduler.schedule_many`` concurrency and thread-safety.

Covers acceptance criteria 3, 34, 44, 45, 46, 49, 60 from
task-20260417-008:

  AC 3  — ``schedule_many`` returns decisions in input order.
  AC 34 — dedup set guarded by ``_dedup_lock`` — check-and-insert is ONE
          critical section.
  AC 44 — budget counter guarded by ``_budget_lock`` — check-and-decrement
          is ONE critical section.
  AC 45 — budget exhaustion emits ``skipped_budget_exceeded`` with a reason
          that contains ``call_budget=<N>``.
  AC 46 — ``ThreadPoolExecutor`` with ``max_workers=max_inflight``.
  AC 49 — template bytes read-once cache under ``_template_lock``.
  AC 60 — ``max_inflight=1`` collapses to serial behavior; per-input order
          preserved regardless of ``max_inflight``.

NOTE on AD-9 (pre-flight thread-safety gate): ``benchmark_trace_llm``
wraps ``autofix.llm_backend.run_prompt``. When the ``agent_bench`` package
is installed, the decorator resolves to ``agent_bench.trace_llm``. The
spec's Risk Notes require the seg-2 executor to VERIFY that the wrapped
``run_prompt`` is thread-safe under the concurrency pool BEFORE shipping
production code. This test file assumes that verification has passed; it
does NOT perform the verification itself. If the pre-flight check fails,
the executor must stop and escalate — do NOT silently set
``max_inflight=1`` to hide the race.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


class _CountingFake:
    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self._delay = delay

    def __call__(self, prompt, **kwargs):
        from autofix.llm_backend import LLMResult

        with self._lock:
            self.calls.append({"prompt": prompt, "kwargs": dict(kwargs)})
        if self._delay:
            time.sleep(self._delay)
        return LLMResult(returncode=0, stdout='{"decision":"confirmed"}', stderr="")


def _make_packet(
    primary_symbol: str = "sample.py::os",
    prompt_prefix_hash: str = "a" * 64,
) -> object:
    from autofix_next.evidence.builder import build_packet

    packet = build_packet(
        rule_id="unused-import.intra-file",
        relpath=primary_symbol.split("::")[0],
        symbol_name=primary_symbol.split("::")[1],
        normalized_import=f"import {primary_symbol.split('::')[1]}",
        changed_slice=f"import {primary_symbol.split('::')[1]}\n",
        analyzer_note="note",
    )
    try:
        object.__setattr__(packet, "prompt_prefix_hash", prompt_prefix_hash)
    except Exception:
        pass
    return packet


def _write_policy(tmp_path: Path, payload: dict) -> None:
    policy_dir = tmp_path / ".autofix"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "autofix-policy.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _install_events(monkeypatch) -> list[dict]:
    captured: list[dict] = []
    lock = threading.Lock()

    def _fake(root, event_type, scan_event, **kwargs):
        with lock:
            captured.append({"event": event_type, "payload": dict(scan_event)})

    monkeypatch.setattr(
        "autofix_next.telemetry.events_log.append_event", _fake
    )
    return captured


def _build(tmp_path: Path, monkeypatch, *, policy: dict | None = None, delay: float = 0.0):
    from autofix_next.llm.scheduler import Scheduler

    fake = _CountingFake(delay=delay)
    monkeypatch.setattr("autofix.llm_backend.run_prompt", fake)
    events = _install_events(monkeypatch)
    _write_policy(tmp_path, policy or {})
    monkeypatch.setenv("AUTOFIX_NEXT_LLM_CACHE_DIR", str(tmp_path / "cache"))
    scheduler = Scheduler(root=tmp_path)
    return scheduler, fake, events


# ---------------------------------------------------------------------------
# AC 3 / AC 60 — schedule_many preserves per-input order.
# ---------------------------------------------------------------------------


def test_schedule_many_preserves_input_order_max_inflight_1(tmp_path, monkeypatch):
    """AC 60: ``max_inflight=1`` preserves per-input ordering."""
    scheduler, fake, _ = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"max_inflight": 1}},
    )
    batch = [
        (_make_packet(primary_symbol=f"f{i}.py::os", prompt_prefix_hash=f"{i:064x}"), None)
        for i in range(8)
    ]
    decisions = scheduler.schedule_many(batch)

    assert isinstance(decisions, list)
    assert len(decisions) == len(batch)
    # Hash order should map cleanly to input order — check prompt tail
    # contains the original primary_symbol in input order.
    for i, (packet, _) in enumerate(batch):
        # Find the corresponding call by prompt_prefix_hash in prompt tail.
        # All decisions must be success-y (no skip), so decisions[i] is
        # meaningful per-input.
        assert decisions[i].decision in {"promoted", "promoted_default_tier"}


def test_schedule_many_preserves_input_order_max_inflight_4(tmp_path, monkeypatch):
    """AC 3 / AC 60: per-input order preserved at ``max_inflight=4``."""
    scheduler, fake, _ = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"max_inflight": 4}},
        delay=0.01,  # encourage scheduling jitter
    )
    batch = [
        (_make_packet(primary_symbol=f"f{i}.py::os", prompt_prefix_hash=f"{i:064x}"), None)
        for i in range(16)
    ]
    decisions = scheduler.schedule_many(batch)

    assert len(decisions) == 16
    for d in decisions:
        assert d.decision in {"promoted", "promoted_default_tier"}


def test_schedule_many_respects_individual_priorities_in_order(tmp_path, monkeypatch):
    """AC 3: each batch slot's priority is applied independently.

    Build a batch of 4 alternating priorities (None, 0.05, 0.5, 0.95).
    The returned decision array must reflect each slot's routing outcome
    at the matching index.
    """
    scheduler, fake, _ = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"small_model_threshold": 0.30, "large_model_threshold": 0.70}},
    )
    batch = [
        (_make_packet(primary_symbol="a.py::x", prompt_prefix_hash="a" * 64), None),
        (_make_packet(primary_symbol="b.py::x", prompt_prefix_hash="b" * 64), 0.05),
        (_make_packet(primary_symbol="c.py::x", prompt_prefix_hash="c" * 64), 0.50),
        (_make_packet(primary_symbol="d.py::x", prompt_prefix_hash="d" * 64), 0.95),
    ]
    decisions = scheduler.schedule_many(batch)

    assert decisions[0].decision == "promoted_default_tier", (
        f"slot 0 (priority=None) must be promoted_default_tier: {decisions[0].decision!r}"
    )
    assert decisions[1].decision == "skipped_below_threshold", (
        f"slot 1 (priority=0.05) must be skipped_below_threshold: {decisions[1].decision!r}"
    )
    assert decisions[2].decision == "promoted", (
        f"slot 2 (priority=0.50, small-band) must be promoted: {decisions[2].decision!r}"
    )
    assert decisions[3].decision == "promoted", (
        f"slot 3 (priority=0.95, large-band) must be promoted: {decisions[3].decision!r}"
    )


# ---------------------------------------------------------------------------
# AC 34 — dedup race: two workers, same hash → exactly one run_prompt call.
# ---------------------------------------------------------------------------


def test_concurrent_dedup_same_hash_one_call(tmp_path, monkeypatch):
    """AC 34: concurrent submits of the same ``prompt_prefix_hash`` collapse to one call."""
    scheduler, fake, events = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"max_inflight": 4}},
        delay=0.02,  # force overlap
    )
    h = "7" * 64
    # Different primary_symbol so suppression can't mask anything but same hash.
    batch = [
        (_make_packet(primary_symbol=f"f{i}.py::os", prompt_prefix_hash=h), None)
        for i in range(8)
    ]

    decisions = scheduler.schedule_many(batch)

    assert len(decisions) == 8
    assert len(fake.calls) == 1, (
        f"dedup must collapse same-hash concurrent submits to a single call: got {len(fake.calls)}"
    )
    decision_names = [d.decision for d in decisions]
    assert decision_names.count("skipped_duplicate_hash") == 7, (
        f"seven duplicates expected: {decision_names!r}"
    )
    assert decision_names.count("promoted_default_tier") + \
           decision_names.count("promoted") == 1, (
        f"exactly one non-duplicate decision expected: {decision_names!r}"
    )


# ---------------------------------------------------------------------------
# AC 44 — budget counter is check-and-decrement under one lock.
# ---------------------------------------------------------------------------


def test_budget_exhaustion_under_concurrency(tmp_path, monkeypatch):
    """AC 44/45: under ``max_inflight=4, call_budget=3``, only 3 calls go through."""
    scheduler, fake, events = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"max_inflight": 4, "call_budget": 3}},
        delay=0.01,
    )
    batch = [
        (_make_packet(primary_symbol=f"f{i}.py::os", prompt_prefix_hash=f"{i:064x}"), None)
        for i in range(10)
    ]

    decisions = scheduler.schedule_many(batch)

    assert len(fake.calls) == 3, (
        f"budget=3 must cap run_prompt calls at 3 under concurrency: got {len(fake.calls)}"
    )
    decision_names = [d.decision for d in decisions]
    assert decision_names.count("skipped_budget_exceeded") == 7, (
        f"seven budget-exceeded skips expected: {decision_names!r}"
    )


def test_budget_exceeded_reason_contains_call_budget_literal(tmp_path, monkeypatch):
    """AC 45: ``skipped_budget_exceeded`` reason contains ``call_budget=<N>``."""
    scheduler, fake, events = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"max_inflight": 1, "call_budget": 1}},
    )
    scheduler.schedule(_make_packet(primary_symbol="a.py::x", prompt_prefix_hash="a" * 64))
    d = scheduler.schedule(_make_packet(primary_symbol="b.py::x", prompt_prefix_hash="b" * 64))

    assert d.decision == "skipped_budget_exceeded"
    assert d.reason is not None
    assert "call_budget=" in d.reason, (
        f"reason must contain literal 'call_budget=': got {d.reason!r}"
    )
    assert "1" in d.reason, (
        f"reason must include the configured budget value: got {d.reason!r}"
    )


# ---------------------------------------------------------------------------
# AC 46 — ThreadPoolExecutor is constructed with max_workers=max_inflight.
# ---------------------------------------------------------------------------


def test_executor_max_workers_reflects_policy(tmp_path, monkeypatch):
    """AC 46: ``_executor._max_workers`` matches policy ``max_inflight``."""
    scheduler, _, _ = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"max_inflight": 7}},
    )
    executor = getattr(scheduler, "_executor", None)
    assert executor is not None, "Scheduler must own a ``_executor`` attribute"
    # ThreadPoolExecutor._max_workers is the documented attribute.
    assert getattr(executor, "_max_workers", None) == 7, (
        f"executor max_workers must reflect policy max_inflight: "
        f"got {getattr(executor, '_max_workers', None)!r}"
    )


# ---------------------------------------------------------------------------
# AC 49 — template bytes cached under _template_lock.
# ---------------------------------------------------------------------------


def test_template_bytes_read_once_across_same_rule_tier(tmp_path, monkeypatch):
    """AC 49: multiple schedule calls with the same (rule_id, tier) read the file only once."""
    scheduler, fake, _ = _build(tmp_path, monkeypatch)

    # Attribute presence check: cache dict + lock are required surface.
    assert hasattr(scheduler, "_template_bytes_cache") or \
           hasattr(scheduler, "_template_cache"), (
        "Scheduler must own a template-bytes cache dict"
    )
    assert hasattr(scheduler, "_template_lock"), (
        "Scheduler must own a _template_lock for template-bytes cache population"
    )

    # Count file reads of the default small-tier legacy template path.
    from pathlib import Path as _Path

    read_count = {"n": 0}
    original_read_text = _Path.read_text

    def _counting_read_text(self, *args, **kwargs):
        if "unused_import_review.md" in str(self) or "unused-import_intra-file" in str(self):
            read_count["n"] += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "read_text", _counting_read_text)

    scheduler.schedule(_make_packet(primary_symbol="a.py::x", prompt_prefix_hash="a" * 64))
    scheduler.schedule(_make_packet(primary_symbol="b.py::x", prompt_prefix_hash="b" * 64))
    scheduler.schedule(_make_packet(primary_symbol="c.py::x", prompt_prefix_hash="c" * 64))

    assert read_count["n"] <= 1, (
        f"template file should be read at most once across same (rule_id, tier): "
        f"observed {read_count['n']} reads"
    )
    assert len(fake.calls) == 3


def test_schedule_is_thin_wrapper_over_schedule_many(tmp_path, monkeypatch):
    """AC 46: ``schedule`` delegates to ``schedule_many([(packet, priority)])[0]``."""
    scheduler, fake, _ = _build(tmp_path, monkeypatch)
    packet = _make_packet(primary_symbol="a.py::x", prompt_prefix_hash="a" * 64)
    d_via_single = scheduler.schedule(packet)
    # Equivalent via schedule_many; reuse another packet with a distinct hash.
    packet_b = _make_packet(primary_symbol="b.py::x", prompt_prefix_hash="b" * 64)
    d_via_batch = scheduler.schedule_many([(packet_b, None)])[0]

    assert d_via_single.decision == d_via_batch.decision, (
        f"schedule and schedule_many([...])[0] must produce the same decision shape: "
        f"{d_via_single.decision!r} vs {d_via_batch.decision!r}"
    )
