"""AC 56 — Scheduler short-circuits at step 7 under ``_REPLAY_ACTIVE=True``.

With the ``_REPLAY_ACTIVE`` ContextVar set to ``True``,
``Scheduler._process_one`` must reach step 7 (promote) and return
``ScheduleDecision(decision='promoted', result=None, reason='replay-mode')``
WITHOUT invoking ``autofix.llm_backend.run_prompt``. We prove the "did
not fire" assertion by monkey-patching ``run_prompt`` to raise — if the
scheduler ever calls it, the exception surfaces in the test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _build_packet(
    primary_symbol: str = "module_b.py::json",
    prompt_prefix_hash: str = "e" * 64,
) -> object:
    """Replicate the ``_make_packet`` shape from test_llm_scheduler.py locally
    so this test does not depend on the frozen fixture file."""
    from autofix.evidence.builder import build_packet

    relpath, symbol = primary_symbol.split("::", 1)
    packet = build_packet(
        rule_id="unused-import.intra-file",
        relpath=relpath,
        symbol_name=symbol,
        normalized_import=f"import {symbol}",
        changed_slice=f"import {symbol}\n",
        analyzer_note="bound name has zero references",
    )
    # Force the hash so dedup can be driven predictably.
    try:
        object.__setattr__(packet, "prompt_prefix_hash", prompt_prefix_hash)
    except Exception:
        pass
    return packet


def _build_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a Scheduler + raise-on-call fake run_prompt.

    Mirrors the helper pattern from tests/autofix/test_llm_scheduler.py
    (frozen; we do not import from it). The run_prompt substitute RAISES
    on invocation so the test fails loudly if step 7 ever reaches the
    LLM seam under replay mode.
    """
    from autofix.llm.scheduler import Scheduler

    def _raising_run_prompt(prompt, **kwargs):
        raise AssertionError(
            "run_prompt must not be invoked when _REPLAY_ACTIVE=True; "
            "scheduler.step-7 replay gate is broken"
        )

    monkeypatch.setattr("autofix.llm_backend.run_prompt", _raising_run_prompt)

    policy_dir = tmp_path / ".autofix"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "autofix-policy.json").write_text(
        json.dumps({}), encoding="utf-8"
    )
    return Scheduler(root=tmp_path)


def test_replay_active_promotes_without_calling_run_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 56: with _REPLAY_ACTIVE=True, schedule() returns
    ScheduleDecision(decision='promoted', result=None, reason='replay-mode')
    and does NOT invoke run_prompt."""
    from autofix.telemetry.replay import _REPLAY_ACTIVE

    scheduler = _build_scheduler(tmp_path, monkeypatch)
    packet = _build_packet()

    token = _REPLAY_ACTIVE.set(True)
    try:
        decision = scheduler.schedule(packet)
    finally:
        _REPLAY_ACTIVE.reset(token)

    assert decision.decision == "promoted", (
        f"expected decision='promoted'; got {decision.decision!r}"
    )
    assert decision.result is None, (
        f"expected result=None under replay mode; got {decision.result!r}"
    )
    assert decision.reason == "replay-mode", (
        f"expected reason='replay-mode'; got {decision.reason!r}"
    )


def test_replay_active_calls_run_prompt_zero_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tightened variant: swap run_prompt for a counter (not a raise) so we
    can assert exactly 0 invocations under replay mode. The raise-variant
    above catches the bug, this variant verifies the call count directly —
    a subtle bug where an exception is swallowed would fail THIS one even
    if the raise-variant above passed silently."""
    from autofix.telemetry.replay import _REPLAY_ACTIVE

    call_count = {"n": 0}

    def _counter(prompt, **kwargs):
        call_count["n"] += 1
        from autofix.llm_backend import LLMResult

        return LLMResult(returncode=0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr("autofix.llm_backend.run_prompt", _counter)

    from autofix.llm.scheduler import Scheduler

    policy_dir = tmp_path / ".autofix"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "autofix-policy.json").write_text(
        json.dumps({}), encoding="utf-8"
    )
    scheduler = Scheduler(root=tmp_path)
    packet = _build_packet(prompt_prefix_hash="f" * 64)

    token = _REPLAY_ACTIVE.set(True)
    try:
        decision = scheduler.schedule(packet)
    finally:
        _REPLAY_ACTIVE.reset(token)

    assert decision.decision == "promoted"
    assert decision.reason == "replay-mode"
    assert call_count["n"] == 0, (
        f"run_prompt must not fire under replay mode; "
        f"observed {call_count['n']} calls"
    )


def test_replay_inactive_still_calls_run_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: with _REPLAY_ACTIVE unset (default False),
    schedule() reaches the real LLM seam. Without this assertion, a
    scheduler that unconditionally returned 'promoted/replay-mode' would
    pass the replay-gate tests while silently breaking normal scans."""
    call_count = {"n": 0}

    def _counter(prompt, **kwargs):
        call_count["n"] += 1
        from autofix.llm_backend import LLMResult

        return LLMResult(returncode=0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr("autofix.llm_backend.run_prompt", _counter)

    from autofix.llm.scheduler import Scheduler

    policy_dir = tmp_path / ".autofix"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "autofix-policy.json").write_text(
        json.dumps({}), encoding="utf-8"
    )
    scheduler = Scheduler(root=tmp_path)
    packet = _build_packet(prompt_prefix_hash="9" * 64)

    decision = scheduler.schedule(packet)
    assert decision.decision in {"promoted", "promoted_default_tier"}, (
        f"non-replay path should reach run_prompt; got {decision.decision!r}"
    )
    assert call_count["n"] == 1, (
        f"non-replay path must invoke run_prompt exactly once; "
        f"observed {call_count['n']}"
    )
    assert decision.reason != "replay-mode", (
        "non-replay decision must not carry 'replay-mode' reason"
    )
