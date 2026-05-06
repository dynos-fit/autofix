"""Tests for Scheduler policy-loading and fail-loud construction paths.

Covers acceptance criteria 12, 15, 16, 17, 22 from task-20260417-008:

  AC 12 — ``generated_disabled=true`` kill switch.
  AC 15 — ``large_threshold < small_threshold`` raises ``ValueError``.
  AC 16 — ``max_inflight <= 0`` raises ``ValueError``.
  AC 17 — ``call_budget <= 0`` raises ``ValueError``; missing/None → 50.
  AC 22 — default fallbacks for missing / non-string / empty ``small_model``
          / ``large_model``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_policy(tmp_path: Path, payload) -> Path:
    policy_dir = tmp_path / ".autofix"
    policy_dir.mkdir(parents=True, exist_ok=True)
    policy_path = policy_dir / "autofix-policy.json"
    if isinstance(payload, str):
        # Let caller inject a pre-built malformed string.
        policy_path.write_text(payload, encoding="utf-8")
    else:
        policy_path.write_text(json.dumps(payload), encoding="utf-8")
    return policy_path


def _set_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOFIX_NEXT_LLM_CACHE_DIR", str(tmp_path / "cache"))


# ---------------------------------------------------------------------------
# AC 15 — large < small → ValueError.
# ---------------------------------------------------------------------------


def test_raises_when_large_threshold_below_small(tmp_path, monkeypatch):
    """AC 15: ``large < small`` is fail-loud at construction."""
    from autofix_next.llm.scheduler import Scheduler

    _set_env(monkeypatch, tmp_path)
    _write_policy(
        tmp_path,
        {
            "llm_tiered": {
                "small_model_threshold": 0.8,
                "large_model_threshold": 0.2,
            }
        },
    )
    with pytest.raises(ValueError):
        Scheduler(root=tmp_path)


# ---------------------------------------------------------------------------
# AC 16 — max_inflight <= 0 → ValueError.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, -100])
def test_raises_when_max_inflight_nonpositive(tmp_path, monkeypatch, value):
    """AC 16: non-positive ``max_inflight`` is fail-loud."""
    from autofix_next.llm.scheduler import Scheduler

    _set_env(monkeypatch, tmp_path)
    _write_policy(tmp_path, {"llm_tiered": {"max_inflight": value}})
    with pytest.raises(ValueError):
        Scheduler(root=tmp_path)


# ---------------------------------------------------------------------------
# AC 17 — call_budget <= 0 → ValueError; missing/None → 50.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, -100])
def test_raises_when_call_budget_nonpositive(tmp_path, monkeypatch, value):
    """AC 17: non-positive ``call_budget`` is fail-loud."""
    from autofix_next.llm.scheduler import Scheduler

    _set_env(monkeypatch, tmp_path)
    _write_policy(tmp_path, {"llm_tiered": {"call_budget": value}})
    with pytest.raises(ValueError):
        Scheduler(root=tmp_path)


def test_call_budget_defaults_to_50_when_missing(tmp_path, monkeypatch):
    """AC 17: missing ``call_budget`` falls back to 50."""
    from autofix_next.llm.scheduler import Scheduler

    _set_env(monkeypatch, tmp_path)
    _write_policy(tmp_path, {})
    scheduler = Scheduler(root=tmp_path)
    # Expose via any of the documented attribute names.
    for attr in ("_budget_remaining", "_call_budget"):
        if hasattr(scheduler, attr):
            assert getattr(scheduler, attr) == 50, (
                f"default call_budget must be 50: {attr}={getattr(scheduler, attr)!r}"
            )
            return
    pytest.fail("Scheduler must expose a budget attribute (_budget_remaining or _call_budget)")


def test_call_budget_none_falls_back_to_default(tmp_path, monkeypatch):
    """AC 17: explicit ``None`` falls back to 50."""
    from autofix_next.llm.scheduler import Scheduler

    _set_env(monkeypatch, tmp_path)
    _write_policy(tmp_path, {"llm_tiered": {"call_budget": None}})
    scheduler = Scheduler(root=tmp_path)
    for attr in ("_budget_remaining", "_call_budget"):
        if hasattr(scheduler, attr):
            assert getattr(scheduler, attr) == 50
            return
    pytest.fail("Scheduler must expose a budget attribute")


# ---------------------------------------------------------------------------
# AC 22 — default fallbacks for small_model / large_model.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy_payload",
    [
        {},
        {"llm_tiered": {}},
        {"llm_tiered": {"small_model": None, "large_model": None}},
        {"llm_tiered": {"small_model": "", "large_model": ""}},
        {"llm_tiered": {"small_model": 42, "large_model": ["sonnet"]}},
    ],
)
def test_model_defaults_at_init(tmp_path, monkeypatch, policy_payload):
    """AC 22: non-string / empty / None / missing values map to defaults."""
    from autofix_next.llm.scheduler import Scheduler

    _set_env(monkeypatch, tmp_path)
    _write_policy(tmp_path, policy_payload)
    scheduler = Scheduler(root=tmp_path)

    assert getattr(scheduler, "_small_model", None) == "haiku", (
        f"small_model must default to 'haiku': got {getattr(scheduler, '_small_model', None)!r}"
    )
    assert getattr(scheduler, "_large_model", None) == "sonnet", (
        f"large_model must default to 'sonnet': got {getattr(scheduler, '_large_model', None)!r}"
    )


def test_malformed_policy_json_raises_value_error(tmp_path, monkeypatch):
    """AC 15 adjacency: malformed policy JSON raises ``ValueError`` at __init__."""
    from autofix_next.llm.scheduler import Scheduler

    _set_env(monkeypatch, tmp_path)
    _write_policy(tmp_path, "{not valid json at all")
    with pytest.raises(ValueError):
        Scheduler(root=tmp_path)


# ---------------------------------------------------------------------------
# AC 12 — ``generated_disabled`` kill switch attribute is wired.
# ---------------------------------------------------------------------------


def test_generated_disabled_attribute_is_true_when_policy_sets_it(tmp_path, monkeypatch):
    """AC 12: policy ``generated_disabled: true`` is observable on Scheduler."""
    from autofix_next.llm.scheduler import Scheduler

    _set_env(monkeypatch, tmp_path)
    _write_policy(tmp_path, {"llm_tiered": {"generated_disabled": True}})
    scheduler = Scheduler(root=tmp_path)
    # Attribute name is part of the private surface; we tolerate either
    # storage shape (flat bool or nested on a policy object).
    disabled = (
        getattr(scheduler, "_generated_disabled", None)
        if hasattr(scheduler, "_generated_disabled")
        else getattr(getattr(scheduler, "_tiered_policy", None), "generated_disabled", None)
    )
    assert disabled is True, (
        f"generated_disabled must flow into Scheduler state: got {disabled!r}"
    )


def test_generated_disabled_default_is_false(tmp_path, monkeypatch):
    """AC 12: default ``generated_disabled`` is False (gate runs)."""
    from autofix_next.llm.scheduler import Scheduler

    _set_env(monkeypatch, tmp_path)
    _write_policy(tmp_path, {})
    scheduler = Scheduler(root=tmp_path)
    disabled = (
        getattr(scheduler, "_generated_disabled", False)
        if hasattr(scheduler, "_generated_disabled")
        else getattr(getattr(scheduler, "_tiered_policy", None), "generated_disabled", False)
    )
    assert disabled is False, (
        f"generated_disabled must default to False: got {disabled!r}"
    )
