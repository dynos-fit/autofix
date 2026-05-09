"""Tests for the persistent prompt-prefix cache inside the Scheduler.

Covers acceptance criteria 36, 37, 38, 39, 40, 41, 42, 43 from
task-20260417-008 plus the AD-10 schema-version kill switch:

  AC 36 — cache-root resolution order: env → policy → default.
  AC 37 — shard layout ``<root>/<hh>/<hash>.json`` with ``[:2]`` prefix.
  AC 38 — entry JSON body shape.
  AC 39 — atomic install (tmp → fsync → os.replace).
  AC 40 — lazy TTL eviction (best-effort unlink).
  AC 41 — ``cache_ttl_seconds`` default 1209600 (14 days).
  AC 42 — cache hit emits ``promoted_cache_hit``, skips run_prompt.
  AC 43 — cache-write OSError → ``cache_store_failed`` + live result.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class _CountingFake:
    def __init__(self, stdout: str = '{"decision":"confirmed"}') -> None:
        self.calls: list[dict] = []
        self._stdout = stdout

    def __call__(self, prompt, **kwargs):
        from autofix.llm_backend import LLMResult

        self.calls.append({"prompt": prompt, "kwargs": dict(kwargs)})
        return LLMResult(returncode=0, stdout=self._stdout, stderr="")


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
    cache_dir: Path | None = None,
    use_env: bool = True,
):
    from autofix.llm.scheduler import Scheduler

    fake = _CountingFake()
    monkeypatch.setattr("autofix.llm_backend.run_prompt", fake)
    events = _install_events(monkeypatch)
    _write_policy(tmp_path, policy or {})
    if use_env:
        resolved = cache_dir if cache_dir is not None else tmp_path / "llm_cache"
        monkeypatch.setenv("AUTOFIX_LLM_CACHE_DIR", str(resolved))
    else:
        monkeypatch.delenv("AUTOFIX_LLM_CACHE_DIR", raising=False)
    scheduler = Scheduler(root=tmp_path)
    return scheduler, fake, events


# ---------------------------------------------------------------------------
# AC 36 — env override wins.
# ---------------------------------------------------------------------------


def test_env_var_overrides_policy_cache_dir(tmp_path, monkeypatch):
    """AC 36: ``AUTOFIX_LLM_CACHE_DIR`` takes precedence over policy key."""
    env_cache = tmp_path / "env_cache"
    policy_cache = tmp_path / "policy_cache"
    monkeypatch.setenv("AUTOFIX_LLM_CACHE_DIR", str(env_cache))
    _write_policy(
        tmp_path,
        {"llm_tiered": {"cache_dir": str(policy_cache)}},
    )
    from autofix.llm.scheduler import Scheduler

    fake = _CountingFake()
    monkeypatch.setattr("autofix.llm_backend.run_prompt", fake)
    _install_events(monkeypatch)

    scheduler = Scheduler(root=tmp_path)
    h = "b" * 64
    scheduler.schedule(_make_packet(prompt_prefix_hash=h))

    # Env-cache root must now contain the entry.
    assert (env_cache / h[:2] / f"{h}.json").is_file(), (
        "env-var cache root must receive the cache entry"
    )
    assert not (policy_cache / h[:2] / f"{h}.json").exists(), (
        "policy-cache root must not be used when env var is set"
    )


def test_policy_cache_dir_used_when_env_unset(tmp_path, monkeypatch):
    """AC 36: with env unset, policy ``cache_dir`` is honored."""
    monkeypatch.delenv("AUTOFIX_LLM_CACHE_DIR", raising=False)
    policy_cache = tmp_path / "policy_cache"
    _write_policy(
        tmp_path,
        {"llm_tiered": {"cache_dir": str(policy_cache)}},
    )
    from autofix.llm.scheduler import Scheduler

    fake = _CountingFake()
    monkeypatch.setattr("autofix.llm_backend.run_prompt", fake)
    _install_events(monkeypatch)

    scheduler = Scheduler(root=tmp_path)
    h = "c" * 64
    scheduler.schedule(_make_packet(prompt_prefix_hash=h))

    assert (policy_cache / h[:2] / f"{h}.json").is_file()


# ---------------------------------------------------------------------------
# AC 37 — shard layout.
# ---------------------------------------------------------------------------


def test_cache_entry_lives_at_sharded_path(tmp_path, monkeypatch):
    """AC 37: entry at ``<root>/<hh>/<hash>.json`` where ``hh = hash[:2]``."""
    scheduler, _, _ = _build(tmp_path, monkeypatch)
    h = "de" + "f" * 62
    scheduler.schedule(_make_packet(prompt_prefix_hash=h))

    expected = tmp_path / "llm_cache" / "de" / f"{h}.json"
    assert expected.is_file(), (
        f"cache entry must live at sharded path: expected={expected}"
    )


# ---------------------------------------------------------------------------
# AC 38 — JSON body shape.
# ---------------------------------------------------------------------------


def test_cache_entry_json_shape(tmp_path, monkeypatch):
    """AC 38: entry JSON has exactly the documented keys with correct types."""
    scheduler, _, _ = _build(tmp_path, monkeypatch)
    h = "ab" + "c" * 62
    scheduler.schedule(_make_packet(prompt_prefix_hash=h))

    path = tmp_path / "llm_cache" / "ab" / f"{h}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_keys = {
        "schema_version",
        "tier",
        "model",
        "returncode",
        "stdout",
        "stderr",
        "created_at_iso",
        "ttl_seconds",
    }
    assert set(payload.keys()) == expected_keys, (
        f"cache entry must contain exactly these keys: "
        f"expected={expected_keys}, got={set(payload.keys())}"
    )
    assert payload["schema_version"] == "llm_cache_v1"
    assert payload["tier"] in {"small", "large"}
    assert isinstance(payload["model"], str)
    assert isinstance(payload["returncode"], int)
    assert isinstance(payload["stdout"], str)
    assert isinstance(payload["stderr"], str)
    assert isinstance(payload["created_at_iso"], str)
    assert isinstance(payload["ttl_seconds"], int)
    # Canonical format per `_now_iso_z`: `YYYY-MM-DDTHH:MM:SSZ`.
    assert payload["created_at_iso"].endswith("Z"), (
        f"created_at_iso must end with Z: {payload['created_at_iso']!r}"
    )
    assert "." not in payload["created_at_iso"], (
        "created_at_iso must not contain fractional seconds"
    )


# ---------------------------------------------------------------------------
# AC 39 — atomic install (tmp → fsync → os.replace).
# ---------------------------------------------------------------------------


def test_cache_write_leaves_no_tmp_file_on_success(tmp_path, monkeypatch):
    """AC 39: after success, only the final file exists; no .tmp leftover."""
    scheduler, _, _ = _build(tmp_path, monkeypatch)
    h = "f" * 64
    scheduler.schedule(_make_packet(prompt_prefix_hash=h))

    shard_dir = tmp_path / "llm_cache" / "ff"
    leftovers = [p for p in shard_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], (
        f".tmp leftover must be cleaned up via os.replace: {leftovers!r}"
    )


# ---------------------------------------------------------------------------
# AC 42 — cache hit short-circuits; no run_prompt invocation.
# ---------------------------------------------------------------------------


def test_cache_hit_short_circuits(tmp_path, monkeypatch):
    """AC 42: a warm cache entry yields ``promoted_cache_hit`` without a call."""
    # Pre-populate a fresh cache entry.
    cache_root = tmp_path / "llm_cache"
    h = "0" * 64
    shard = cache_root / h[:2]
    shard.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema_version": "llm_cache_v1",
        "tier": "small",
        "model": "haiku",
        "returncode": 0,
        "stdout": '{"decision":"cached"}',
        "stderr": "",
        "created_at_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl_seconds": 1209600,
    }
    (shard / f"{h}.json").write_text(json.dumps(entry), encoding="utf-8")

    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet(prompt_prefix_hash=h))

    assert decision.decision == "promoted_cache_hit"
    assert decision.result is not None, "cache hit must reconstruct an LLMResult"
    assert decision.result.returncode == 0
    assert decision.result.stdout == '{"decision":"cached"}'
    assert fake.calls == [], "cache hit must not invoke run_prompt"
    names = [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]
    assert names.count("promoted_cache_hit") == 1, (
        f"exactly one promoted_cache_hit event expected: {names!r}"
    )


# ---------------------------------------------------------------------------
# AC 40 / AC 41 — TTL eviction (lazy + default 14 days).
# ---------------------------------------------------------------------------


def test_default_cache_ttl_is_14_days(tmp_path, monkeypatch):
    """AC 41: a freshly-written entry has ttl_seconds == 1209600 by default."""
    scheduler, _, _ = _build(tmp_path, monkeypatch)
    h = "1" * 64
    scheduler.schedule(_make_packet(prompt_prefix_hash=h))

    entry = json.loads(
        (tmp_path / "llm_cache" / "11" / f"{h}.json").read_text(encoding="utf-8")
    )
    assert entry["ttl_seconds"] == 1209600, (
        f"default ttl_seconds must be 1209600 (14 days): got {entry['ttl_seconds']}"
    )


def test_expired_cache_entry_is_treated_as_miss_and_unlinked(tmp_path, monkeypatch):
    """AC 40: expired entry → miss + best-effort unlink + fresh run_prompt call."""
    cache_root = tmp_path / "llm_cache"
    h = "2" * 64
    shard = cache_root / h[:2]
    shard.mkdir(parents=True, exist_ok=True)
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=3600)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    entry = {
        "schema_version": "llm_cache_v1",
        "tier": "small",
        "model": "haiku",
        "returncode": 0,
        "stdout": '{"decision":"stale"}',
        "stderr": "",
        "created_at_iso": stale_ts,
        "ttl_seconds": 60,  # expired 59 minutes ago
    }
    path = shard / f"{h}.json"
    path.write_text(json.dumps(entry), encoding="utf-8")

    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet(prompt_prefix_hash=h))

    assert decision.decision != "promoted_cache_hit", (
        f"expired entry must not be returned as a hit: {decision.decision!r}"
    )
    assert len(fake.calls) == 1, "expired entry must force a fresh run_prompt call"
    # Expired entry was unlinked before being overwritten by the fresh cache put.
    # We can't observe the unlink directly, but after the fresh put, the file
    # must now carry the live `created_at_iso`, not the stale one.
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["created_at_iso"] != stale_ts, (
        "expired entry must have been replaced (unlinked then rewritten)"
    )


# ---------------------------------------------------------------------------
# AC 43 — cache-write OSError → cache_store_failed + live result.
# ---------------------------------------------------------------------------


def test_cache_store_failed_emits_event_and_returns_live_result(tmp_path, monkeypatch):
    """AC 43: OSError during cache write emits ``cache_store_failed``.

    The live ``LLMResult`` is still returned in ``ScheduleDecision(decision=
    "promoted", ...)``. The scheduler does NOT re-raise.
    """
    scheduler, fake, events = _build(tmp_path, monkeypatch)

    real_replace = os.replace

    def _raising_replace(src, dst, *args, **kwargs):
        # Make the first cache replace explode; everything else continues.
        raise OSError("simulated cache write failure")

    monkeypatch.setattr(os, "replace", _raising_replace)

    decision = scheduler.schedule(_make_packet(prompt_prefix_hash="9" * 64))

    # Live LLMResult still surfaced to caller.
    assert decision.decision == "promoted", (
        f"failed cache write must still surface the live LLMResult as promoted: "
        f"got decision={decision.decision!r}"
    )
    assert decision.result is not None
    assert decision.result.returncode == 0

    names = [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]
    assert "cache_store_failed" in names, (
        f"cache_store_failed event must be emitted: {names!r}"
    )
    # And one promoted event for the live result (AC 43 says both; the spec
    # says "still returns the live LLMResult" and emits one cache_store_failed
    # event).
    assert names.count("cache_store_failed") == 1, (
        f"exactly one cache_store_failed event expected: {names!r}"
    )

    # Restore for any downstream teardown.
    monkeypatch.setattr(os, "replace", real_replace)


# ---------------------------------------------------------------------------
# AD-10 — schema-version mismatch is a kill switch (one-bit bump → full miss).
# ---------------------------------------------------------------------------


def test_schema_version_mismatch_is_a_miss(tmp_path, monkeypatch):
    """AD-10 kill switch: entries with ``schema_version != 'llm_cache_v1'`` are a miss."""
    cache_root = tmp_path / "llm_cache"
    h = "3" * 64
    shard = cache_root / h[:2]
    shard.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema_version": "llm_cache_v0",  # old/future schema
        "tier": "small",
        "model": "haiku",
        "returncode": 0,
        "stdout": '{"decision":"stale"}',
        "stderr": "",
        "created_at_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl_seconds": 1209600,
    }
    (shard / f"{h}.json").write_text(json.dumps(entry), encoding="utf-8")

    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet(prompt_prefix_hash=h))

    assert decision.decision != "promoted_cache_hit", (
        "wrong-schema entry must NOT be returned as a cache hit"
    )
    assert len(fake.calls) == 1, (
        "wrong-schema entry must force a fresh run_prompt"
    )
    names = [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]
    assert "promoted_cache_hit" not in names


# ---------------------------------------------------------------------------
# AC 42 negative: corrupt / bad JSON cache entries.
# ---------------------------------------------------------------------------


def test_bad_json_cache_entry_is_treated_as_miss(tmp_path, monkeypatch):
    """Bad JSON bytes in a cache file → miss (no hit, no crash)."""
    cache_root = tmp_path / "llm_cache"
    h = "4" * 64
    shard = cache_root / h[:2]
    shard.mkdir(parents=True, exist_ok=True)
    (shard / f"{h}.json").write_text("{this is not valid json", encoding="utf-8")

    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet(prompt_prefix_hash=h))

    assert decision.decision != "promoted_cache_hit"
    assert len(fake.calls) == 1
    names = [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]
    assert "promoted_cache_hit" not in names


def test_unparseable_created_at_iso_is_a_miss(tmp_path, monkeypatch):
    """Cache entry with fractional seconds / no-Z is a miss (per assumption)."""
    cache_root = tmp_path / "llm_cache"
    h = "5" * 64
    shard = cache_root / h[:2]
    shard.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema_version": "llm_cache_v1",
        "tier": "small",
        "model": "haiku",
        "returncode": 0,
        "stdout": '{"decision":"stale"}',
        "stderr": "",
        "created_at_iso": "2026-04-17T12:34:56.789",  # fractional, no Z
        "ttl_seconds": 1209600,
    }
    (shard / f"{h}.json").write_text(json.dumps(entry), encoding="utf-8")

    scheduler, fake, _ = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet(prompt_prefix_hash=h))

    assert decision.decision != "promoted_cache_hit"
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# AC 44 partial — cache hit does NOT consume budget.
# ---------------------------------------------------------------------------


def test_cache_hit_does_not_consume_budget(tmp_path, monkeypatch):
    """AC 44: a cache hit must not decrement the call budget.

    With ``call_budget=1``, a warm cache entry on packet A should leave the
    budget available for packet B to actually call ``run_prompt``.
    """
    cache_root = tmp_path / "llm_cache"
    h_a = "a" * 64
    shard_a = cache_root / h_a[:2]
    shard_a.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema_version": "llm_cache_v1",
        "tier": "small",
        "model": "haiku",
        "returncode": 0,
        "stdout": '{"decision":"cached"}',
        "stderr": "",
        "created_at_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl_seconds": 1209600,
    }
    (shard_a / f"{h_a}.json").write_text(json.dumps(entry), encoding="utf-8")

    scheduler, fake, _ = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"call_budget": 1}},
    )

    d1 = scheduler.schedule(
        _make_packet(primary_symbol="a.py::os", prompt_prefix_hash=h_a)
    )
    d2 = scheduler.schedule(
        _make_packet(primary_symbol="b.py::os", prompt_prefix_hash="b" * 64)
    )

    assert d1.decision == "promoted_cache_hit"
    assert d2.decision in {"promoted", "promoted_default_tier"}, (
        f"budget must not have been consumed by the cache hit: {d2.decision!r}"
    )
    assert len(fake.calls) == 1, (
        f"only the non-cached packet should reach run_prompt: calls={len(fake.calls)}"
    )
