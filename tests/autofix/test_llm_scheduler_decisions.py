"""Parametrized ten-decision matrix and system-level invariants.

Covers acceptance criteria 5, 50, 51, 53, 57, 58, 59 from task-20260417-008:

  AC 5  — ``ScheduleDecisionName`` Literal contains exactly ten members.
  AC 50 — ``run_prompt`` exception → ``promoted_failed`` with reason
          ``f"{type(exc).__name__}: {exc}"``.
  AC 51 — non-``LLMResult`` / non-zero returncode → ``promoted_failed``
          with reason containing the substring ``"returncode="``.
  AC 53 — no code path creates/modifies files under ``.autofix/state/**``.
  AC 57 — every emitted event carries the base payload keys plus
          ``reason`` for any decision other than ``promoted`` / ``promoted_cache_hit``.
  AC 58 — ``NEW_EVENT_NAMES`` unchanged; no new event-name is added.
  AC 59 — each of the ten decisions has a positive emission test AND a
          negative (NOT emitted on non-matching gate) test.
"""

from __future__ import annotations

import json
import os
import typing as _t
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_EXPECTED_DECISION_NAMES: set[str] = {
    "promoted",
    "skipped_suppressed",
    "skipped_duplicate_hash",
    "promoted_failed",
    "skipped_generated",
    "skipped_below_threshold",
    "skipped_budget_exceeded",
    "promoted_cache_hit",
    "promoted_default_tier",
    "cache_store_failed",
}


class _CountingFake:
    def __init__(
        self,
        *,
        raise_exc: Exception | None = None,
        return_value=None,
    ) -> None:
        self.calls: list[dict] = []
        self._raise = raise_exc
        self._return = return_value

    def __call__(self, prompt, **kwargs):
        from autofix.llm_backend import LLMResult

        self.calls.append({"prompt": prompt, "kwargs": dict(kwargs)})
        if self._raise is not None:
            raise self._raise
        if self._return is not None:
            return self._return
        return LLMResult(returncode=0, stdout='{"decision":"confirmed"}', stderr="")


def _make_packet(
    primary_symbol: str = "sample.py::os",
    prompt_prefix_hash: str = "a" * 64,
) -> object:
    from autofix.evidence.builder import build_packet

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

    def _fake(root, event_type, scan_event, **kwargs):
        captured.append({"event": event_type, "payload": dict(scan_event)})

    monkeypatch.setattr(
        "autofix.telemetry.events_log.append_event", _fake
    )
    return captured


def _build(
    tmp_path: Path,
    monkeypatch,
    *,
    policy: dict | None = None,
    fake: _CountingFake | None = None,
):
    from autofix.llm.scheduler import Scheduler

    fake = fake or _CountingFake()
    monkeypatch.setattr("autofix.llm_backend.run_prompt", fake)
    events = _install_events(monkeypatch)
    _write_policy(tmp_path, policy or {})
    monkeypatch.setenv("AUTOFIX_LLM_CACHE_DIR", str(tmp_path / "cache"))
    scheduler = Scheduler(root=tmp_path)
    return scheduler, fake, events


def _decisions(events: list[dict]) -> list[str]:
    return [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]


# ---------------------------------------------------------------------------
# AC 5 — ten-member decision literal.
# ---------------------------------------------------------------------------


def test_schedule_decision_name_has_exactly_ten_members():
    """AC 5: the Literal's args set is exactly the ten documented names."""
    from autofix.llm.scheduler import ScheduleDecisionName

    args = set(_t.get_args(ScheduleDecisionName))
    assert args == _EXPECTED_DECISION_NAMES, (
        f"ScheduleDecisionName must contain exactly these ten names: "
        f"missing={_EXPECTED_DECISION_NAMES - args}, extra={args - _EXPECTED_DECISION_NAMES}"
    )


# ---------------------------------------------------------------------------
# Positive emission, one per decision.
# ---------------------------------------------------------------------------


def test_emits_promoted_on_successful_explicit_priority(tmp_path, monkeypatch):
    """AC 59: ``promoted`` is emitted on a successful small-band call."""
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet(), priority=0.50)

    assert decision.decision == "promoted"
    assert "promoted" in _decisions(events)
    assert len(fake.calls) == 1


def test_emits_promoted_default_tier_on_priority_none(tmp_path, monkeypatch):
    """AC 59: ``promoted_default_tier`` is emitted for ``priority=None``."""
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet())

    assert decision.decision == "promoted_default_tier"
    assert "promoted_default_tier" in _decisions(events)


def test_promoted_default_tier_event_carries_reason(tmp_path, monkeypatch):
    """AC 57: promoted_default_tier LLMCallGated payload must include a non-null reason."""
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    scheduler.schedule(_make_packet())

    gated = [e for e in events if e["event"] == "LLMCallGated"]
    tier_events = [
        e for e in gated
        if e["payload"].get("decision") == "promoted_default_tier"
    ]
    assert tier_events, "expected at least one promoted_default_tier LLMCallGated event"
    for e in tier_events:
        reason = e["payload"].get("reason")
        assert reason, (
            f"promoted_default_tier payload must carry a non-null reason; got: {e['payload']!r}"
        )


def test_emits_skipped_suppressed_on_glob_match(tmp_path, monkeypatch):
    """AC 59: ``skipped_suppressed`` fires on a suppression match."""
    scheduler, fake, events = _build(
        tmp_path,
        monkeypatch,
        policy={"suppressions": ["blocked/**"]},
    )
    decision = scheduler.schedule(
        _make_packet(primary_symbol="blocked/foo.py::os")
    )
    assert decision.decision == "skipped_suppressed"
    assert "skipped_suppressed" in _decisions(events)
    assert fake.calls == []


def test_emits_skipped_generated_on_marker_hit(tmp_path, monkeypatch):
    """AC 59: ``skipped_generated`` fires on a file with a generated marker."""
    target = tmp_path / "pkg" / "models.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# @generated by schema\n", encoding="utf-8")

    scheduler, fake, events = _build(tmp_path, monkeypatch)
    scheduler.schedule(_make_packet(primary_symbol="pkg/models.py::x"))
    assert "skipped_generated" in _decisions(events)
    assert fake.calls == []


def test_emits_skipped_below_threshold_on_low_priority(tmp_path, monkeypatch):
    """AC 59: ``skipped_below_threshold`` fires on priority below small-threshold."""
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    scheduler.schedule(_make_packet(), priority=0.01)
    assert "skipped_below_threshold" in _decisions(events)
    assert fake.calls == []


def test_emits_skipped_duplicate_hash_on_second_same_hash(tmp_path, monkeypatch):
    """AC 59: ``skipped_duplicate_hash`` fires on a second same-hash packet."""
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    h = "d" * 64
    scheduler.schedule(_make_packet(primary_symbol="a.py::x", prompt_prefix_hash=h))
    scheduler.schedule(_make_packet(primary_symbol="b.py::x", prompt_prefix_hash=h))
    assert "skipped_duplicate_hash" in _decisions(events)


def test_emits_skipped_budget_exceeded_when_budget_zero(tmp_path, monkeypatch):
    """AC 59: ``skipped_budget_exceeded`` fires when the budget is exhausted."""
    scheduler, fake, events = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"call_budget": 1}},
    )
    scheduler.schedule(_make_packet(primary_symbol="a.py::x", prompt_prefix_hash="a" * 64))
    scheduler.schedule(_make_packet(primary_symbol="b.py::x", prompt_prefix_hash="b" * 64))
    names = _decisions(events)
    assert "skipped_budget_exceeded" in names


def test_emits_promoted_cache_hit_when_cache_warm(tmp_path, monkeypatch):
    """AC 59: ``promoted_cache_hit`` fires on a fresh warm cache entry."""
    cache_root = tmp_path / "cache"
    h = "e" * 64
    shard = cache_root / h[:2]
    shard.mkdir(parents=True, exist_ok=True)
    (shard / f"{h}.json").write_text(
        json.dumps(
            {
                "schema_version": "llm_cache_v1",
                "tier": "small",
                "model": "haiku",
                "returncode": 0,
                "stdout": '{"decision":"cached"}',
                "stderr": "",
                "created_at_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ttl_seconds": 1209600,
            }
        ),
        encoding="utf-8",
    )
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet(prompt_prefix_hash=h))
    assert decision.decision == "promoted_cache_hit"
    assert "promoted_cache_hit" in _decisions(events)
    assert fake.calls == []


def test_emits_promoted_failed_on_exception(tmp_path, monkeypatch):
    """AC 50 / AC 59: ``promoted_failed`` fires on exception from run_prompt."""
    fake = _CountingFake(raise_exc=RuntimeError("boom!"))
    scheduler, _, events = _build(tmp_path, monkeypatch, fake=fake)
    decision = scheduler.schedule(_make_packet())
    assert decision.decision == "promoted_failed"
    assert decision.reason is not None
    assert decision.reason == "RuntimeError: boom!", (
        f"reason must equal f'{{type(exc).__name__}}: {{exc}}': got {decision.reason!r}"
    )
    gated = [e for e in events if e["event"] == "LLMCallGated"]
    failed_events = [e for e in gated if e["payload"].get("decision") == "promoted_failed"]
    assert len(failed_events) == 1
    assert failed_events[0]["payload"].get("reason") == "RuntimeError: boom!"


def test_emits_promoted_failed_on_nonzero_returncode(tmp_path, monkeypatch):
    """AC 51: ``promoted_failed`` reason contains ``returncode=`` on bad rc."""
    from autofix.llm_backend import LLMResult

    fake = _CountingFake(return_value=LLMResult(returncode=2, stdout="", stderr="err"))
    scheduler, _, events = _build(tmp_path, monkeypatch, fake=fake)
    decision = scheduler.schedule(_make_packet())

    assert decision.decision == "promoted_failed"
    assert decision.reason is not None
    assert "returncode=" in decision.reason
    assert "2" in decision.reason
    gated = [e for e in events if e["event"] == "LLMCallGated"]
    failed_events = [e for e in gated if e["payload"].get("decision") == "promoted_failed"]
    assert len(failed_events) == 1
    assert "returncode=" in failed_events[0]["payload"].get("reason", "")


def test_emits_promoted_failed_on_non_llmresult_return(tmp_path, monkeypatch):
    """AC 51: non-``LLMResult`` return emits ``promoted_failed`` with ``returncode=``."""
    fake = _CountingFake(return_value="unexpected string return")
    scheduler, _, events = _build(tmp_path, monkeypatch, fake=fake)
    decision = scheduler.schedule(_make_packet())

    assert decision.decision == "promoted_failed"
    assert decision.reason is not None
    assert "returncode=" in decision.reason


def test_emits_cache_store_failed_on_os_error(tmp_path, monkeypatch):
    """AC 43 / AC 59: ``cache_store_failed`` fires when cache write raises OSError."""
    scheduler, fake, events = _build(tmp_path, monkeypatch)

    def _raising_replace(src, dst, *args, **kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "replace", _raising_replace)

    decision = scheduler.schedule(_make_packet(prompt_prefix_hash="9" * 64))
    assert decision.decision == "promoted"
    assert decision.result is not None
    assert "cache_store_failed" in _decisions(events)


# ---------------------------------------------------------------------------
# Negative emission: each decision is NOT emitted on at least one
# non-matching gate path. AC 59 is half-positive, half-negative.
# ---------------------------------------------------------------------------


def test_promoted_not_emitted_on_suppression_path(tmp_path, monkeypatch):
    """AC 59 (neg): ``promoted`` is NOT emitted when suppression fires."""
    scheduler, fake, events = _build(
        tmp_path,
        monkeypatch,
        policy={"suppressions": ["blocked/**"]},
    )
    scheduler.schedule(_make_packet(primary_symbol="blocked/f.py::x"), priority=0.5)
    assert "promoted" not in _decisions(events)
    assert fake.calls == []


def test_promoted_default_tier_not_emitted_with_explicit_priority(tmp_path, monkeypatch):
    """AC 48 / AC 59 (neg): explicit ``priority`` never emits ``promoted_default_tier``."""
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    scheduler.schedule(_make_packet(), priority=0.50)
    assert "promoted_default_tier" not in _decisions(events)


def test_skipped_suppressed_not_emitted_on_clean_path(tmp_path, monkeypatch):
    """AC 59 (neg): ``skipped_suppressed`` NOT emitted when no glob matches."""
    scheduler, fake, events = _build(
        tmp_path,
        monkeypatch,
        policy={"suppressions": ["other/**"]},
    )
    scheduler.schedule(_make_packet(primary_symbol="clean.py::x"))
    assert "skipped_suppressed" not in _decisions(events)


def test_skipped_duplicate_hash_not_emitted_on_first_hash(tmp_path, monkeypatch):
    """AC 59 (neg): first-seen hash does NOT emit ``skipped_duplicate_hash``."""
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    scheduler.schedule(_make_packet(prompt_prefix_hash="7" * 64))
    assert "skipped_duplicate_hash" not in _decisions(events)


def test_promoted_failed_not_emitted_on_clean_success(tmp_path, monkeypatch):
    """AC 59 (neg): successful path does NOT emit ``promoted_failed``."""
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    scheduler.schedule(_make_packet(prompt_prefix_hash="8" * 64))
    assert "promoted_failed" not in _decisions(events)


def test_skipped_generated_not_emitted_on_clean_file(tmp_path, monkeypatch):
    """AC 59 (neg): clean file does NOT emit ``skipped_generated``."""
    target = tmp_path / "normal.py"
    target.write_text("import os\nprint(os.getcwd())\n", encoding="utf-8")
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    scheduler.schedule(_make_packet(primary_symbol="normal.py::os"))
    assert "skipped_generated" not in _decisions(events)


def test_skipped_below_threshold_not_emitted_on_high_priority(tmp_path, monkeypatch):
    """AC 59 (neg): ``priority >= small_threshold`` does NOT emit ``skipped_below_threshold``."""
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    scheduler.schedule(_make_packet(), priority=0.95)
    assert "skipped_below_threshold" not in _decisions(events)


def test_skipped_budget_exceeded_not_emitted_under_budget(tmp_path, monkeypatch):
    """AC 59 (neg): with plenty of budget, ``skipped_budget_exceeded`` does NOT fire."""
    scheduler, fake, events = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"call_budget": 100}},
    )
    scheduler.schedule(_make_packet())
    assert "skipped_budget_exceeded" not in _decisions(events)


def test_promoted_cache_hit_not_emitted_on_cold_cache(tmp_path, monkeypatch):
    """AC 59 (neg): a cold cache does NOT emit ``promoted_cache_hit``."""
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    scheduler.schedule(_make_packet())
    assert "promoted_cache_hit" not in _decisions(events)


def test_cache_store_failed_not_emitted_on_successful_write(tmp_path, monkeypatch):
    """AC 59 (neg): successful cache write does NOT emit ``cache_store_failed``."""
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    scheduler.schedule(_make_packet())
    assert "cache_store_failed" not in _decisions(events)


# ---------------------------------------------------------------------------
# AC 57 — payload shape: base keys + conditional ``reason``.
# ---------------------------------------------------------------------------


_BASE_KEYS: set[str] = {
    "event_type",
    "repo_id",
    "decision",
    "prompt_prefix_hash",
    "primary_symbol",
    "rule_id",
}


def test_llmcallgated_payloads_have_required_base_keys(tmp_path, monkeypatch):
    """AC 57: every ``LLMCallGated`` event carries the base-key set."""
    scheduler, _, events = _build(tmp_path, monkeypatch)
    scheduler.schedule(_make_packet())

    gated = [e for e in events if e["event"] == "LLMCallGated"]
    assert gated, "at least one LLMCallGated event expected"
    for e in gated:
        missing = _BASE_KEYS - set(e["payload"].keys())
        assert not missing, (
            f"LLMCallGated payload missing base keys {missing!r}: {e['payload']!r}"
        )


def test_reason_present_on_non_promoted_decisions(tmp_path, monkeypatch):
    """AC 57: any decision other than ``promoted`` / ``promoted_cache_hit`` carries ``reason``."""
    # Suppressed path — reason is required.
    scheduler, _, events = _build(
        tmp_path,
        monkeypatch,
        policy={"suppressions": ["blocked/**"]},
    )
    scheduler.schedule(_make_packet(primary_symbol="blocked/f.py::x"))

    gated = [e for e in events if e["event"] == "LLMCallGated"]
    sup = [e for e in gated if e["payload"].get("decision") == "skipped_suppressed"]
    assert sup, "expected a skipped_suppressed event"
    for e in sup:
        assert "reason" in e["payload"], (
            f"skipped_suppressed payload must include 'reason': {e['payload']!r}"
        )


# ---------------------------------------------------------------------------
# AC 58 — NEW_EVENT_NAMES invariance.
# ---------------------------------------------------------------------------


def test_new_event_names_contains_llmcallgated_and_has_not_grown():
    """AC 58: ``LLMCallGated`` is the only event-name carrier for new decisions."""
    from autofix.events.schema import NEW_EVENT_NAMES

    assert "LLMCallGated" in NEW_EVENT_NAMES

    # Frozen set of event names. New decisions still ride on the existing
    # LLMCallGated event; no new LLM-gate names.
    expected_baseline = {
        "ScanStarted",
        "SymbolIndexed",
        "EvidencePacketBuilt",
        "LLMCallGated",
        "SARIFEmitted",
        "ScanCompleted",
        "InvalidationComputed",
        "PriorityScored",
        "FindingDeduped",
        "ClusterStorePersisted",
        "AdapterRegistered",
        "AdapterPrecisionUnavailable",
        "LanguageShardPersisted",
        "ScanExplanation",
        "TracerProviderConfigurationFailed",
        # added by task-20260506-005 (ARCH-001) — linter-passthrough analyzer events
        "AnalyzerUnavailable",
        "AnalyzerTimeout",
        "AnalyzerError",
        "AnalyzerUnknown",
        "AnalyzerSkipped",
        "LLMPatchInvoked",
        "LLMPatchProduced",
        "LLMPatchRejected",
        "LLMPatchBudgetExceeded",
    }
    assert set(NEW_EVENT_NAMES) == expected_baseline, (
        f"NEW_EVENT_NAMES must match the task-012 baseline: "
        f"new={set(NEW_EVENT_NAMES) - expected_baseline}, "
        f"removed={expected_baseline - set(NEW_EVENT_NAMES)}"
    )


# ---------------------------------------------------------------------------
# AC 53 — no writes under ``.autofix/state/**``.
# ---------------------------------------------------------------------------


def test_scheduler_does_not_write_under_autofix_state(tmp_path, monkeypatch):
    """AC 53: a full scan cycle never creates files under ``.autofix/state/**``."""
    # Ensure there's no pre-existing state dir.
    state_dir = tmp_path / ".autofix" / "state"
    assert not state_dir.exists()

    scheduler, fake, _ = _build(tmp_path, monkeypatch)

    # Drive a variety of code paths.
    batch = [
        (_make_packet(primary_symbol="a.py::x", prompt_prefix_hash="1" * 64), None),
        (_make_packet(primary_symbol="b.py::x", prompt_prefix_hash="2" * 64), 0.95),
        (_make_packet(primary_symbol="c.py::x", prompt_prefix_hash="3" * 64), 0.05),
    ]
    scheduler.schedule_many(batch)

    # After the scan, assert no `.autofix/state/**` files came into being.
    if state_dir.exists():
        contents = list(state_dir.rglob("*"))
        pytest.fail(
            f"Scheduler must not create .autofix/state/**: found {contents!r}"
        )
