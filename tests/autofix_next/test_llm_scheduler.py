"""Tests for autofix_next.llm.scheduler.Scheduler.

Covers:
  AC #7  — exactly one run_prompt call per promoted candidate.
  AC #8  — run_prompt is the sole LLM seam in autofix_next/.
  AC #14 — suppressed path → skipped_suppressed, no run_prompt call.
  AC #15 — duplicate prompt_prefix_hash → skipped_duplicate_hash.
  AC #24 — prompt assembled as template Markdown FIRST, then JSON packet LAST.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


class _CountingFake:
    """A fake replacement for autofix.llm_backend.run_prompt that records
    every invocation and returns a fixed LLMResult-like object."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, prompt, **kwargs):
        from autofix.llm_backend import LLMResult

        self.calls.append({"prompt": prompt, "kwargs": dict(kwargs)})
        return LLMResult(returncode=0, stdout='{"decision":"confirmed"}', stderr="")


def _make_packet(
    primary_symbol: str = "sample.py::os",
    prompt_prefix_hash: str = "a" * 64,
    finding_id: str = "f" * 64,
) -> object:
    """Construct a minimal evidence packet. We import the real schema so the
    scheduler sees a live object, but we supply the primitive fields by name."""
    from autofix_next.evidence.builder import build_packet

    packet = build_packet(
        rule_id="unused-import.intra-file",
        relpath=primary_symbol.split("::")[0],
        symbol_name=primary_symbol.split("::")[1],
        normalized_import=f"import {primary_symbol.split('::')[1]}",
        changed_slice=f"import {primary_symbol.split('::')[1]}\n",
        analyzer_note="bound name has zero identifier references",
    )
    # Force the hash / fingerprint fields so we can drive dedup behavior.
    if hasattr(packet, "prompt_prefix_hash"):
        try:
            object.__setattr__(packet, "prompt_prefix_hash", prompt_prefix_hash)
        except Exception:
            pass
    if hasattr(packet, "finding_id"):
        try:
            object.__setattr__(packet, "finding_id", finding_id)
        except Exception:
            pass
    return packet


def _build_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                     suppressions: list[str] | None = None) -> tuple[object, _CountingFake]:
    """Build a Scheduler whose .autofix/autofix-policy.json lives under tmp_path."""
    from autofix_next.llm.scheduler import Scheduler

    fake = _CountingFake()
    monkeypatch.setattr("autofix.llm_backend.run_prompt", fake)

    policy_dir = tmp_path / ".autofix"
    policy_dir.mkdir(parents=True, exist_ok=True)
    policy_path = policy_dir / "autofix-policy.json"
    policy_path.write_text(
        json.dumps({"suppressions": list(suppressions or [])}),
        encoding="utf-8",
    )

    scheduler = Scheduler(root=tmp_path)
    return scheduler, fake


def test_run_prompt_called_exactly_once_per_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #7: one promoted packet → exactly one run_prompt call."""
    scheduler, fake = _build_scheduler(tmp_path, monkeypatch)

    packet = _make_packet(prompt_prefix_hash="a" * 64)
    scheduler.schedule(packet)

    assert len(fake.calls) == 1, f"expected 1 call, got {len(fake.calls)}: {fake.calls!r}"


def test_run_prompt_is_only_llm_seam() -> None:
    """AC #8: the only LLM-invocation surface in autofix_next/ is an import
    of autofix.llm_backend.run_prompt inside autofix_next/llm/scheduler.py.
    No other module references run_prompt, claude, or invokes an HTTP client
    for LLM purposes."""
    pkg = REPO_ROOT / "autofix_next"
    assert pkg.is_dir(), f"autofix_next/ must exist: {pkg}"

    proc = subprocess.run(
        [
            "grep",
            "-R",
            "-n",
            "--include=*.py",
            "-E",
            "run_prompt|claude",
            str(pkg),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        pytest.fail(f"grep failed: rc={proc.returncode} stderr={proc.stderr!r}")

    scheduler_path = (pkg / "llm" / "scheduler.py").resolve()
    offending: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        path_part = line.split(":", 1)[0]
        if Path(path_part).resolve() != scheduler_path:
            offending.append(line)

    assert not offending, (
        "Only autofix_next/llm/scheduler.py may reference run_prompt/claude:\n"
        + "\n".join(offending)
    )


def test_suppressed_path_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #14: packet whose primary_symbol path matches a suppression glob
    is NOT passed to run_prompt; an LLMCallGated event with decision
    'skipped_suppressed' is emitted instead."""
    emitted_events: list[dict] = []

    def _fake_append_event(root, event_type, scan_event):
        emitted_events.append({"event": event_type, "payload": dict(scan_event)})

    monkeypatch.setattr(
        "autofix_next.telemetry.events_log.append_event", _fake_append_event
    )

    scheduler, fake = _build_scheduler(
        tmp_path, monkeypatch, suppressions=["suppressed/**"]
    )

    packet = _make_packet(primary_symbol="suppressed/foo.py::os")
    scheduler.schedule(packet)

    assert fake.calls == [], f"run_prompt must not be called: {fake.calls!r}"
    decisions = [e["payload"].get("decision") for e in emitted_events
                 if e["event"] == "LLMCallGated"]
    assert "skipped_suppressed" in decisions, f"events: {emitted_events!r}"


def test_duplicate_hash_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #15: a second packet with the same prompt_prefix_hash within one
    scan is skipped with decision 'skipped_duplicate_hash'."""
    emitted_events: list[dict] = []

    def _fake_append_event(root, event_type, scan_event):
        emitted_events.append({"event": event_type, "payload": dict(scan_event)})

    monkeypatch.setattr(
        "autofix_next.telemetry.events_log.append_event", _fake_append_event
    )

    scheduler, fake = _build_scheduler(tmp_path, monkeypatch)

    h = "c" * 64
    scheduler.schedule(_make_packet(prompt_prefix_hash=h,
                                    primary_symbol="a.py::os"))
    scheduler.schedule(_make_packet(prompt_prefix_hash=h,
                                    primary_symbol="b.py::os"))

    assert len(fake.calls) == 1, (
        f"duplicate hash must collapse to one call, got {len(fake.calls)}"
    )
    decisions = [
        e["payload"].get("decision")
        for e in emitted_events
        if e["event"] == "LLMCallGated"
    ]
    assert "skipped_duplicate_hash" in decisions, f"events: {emitted_events!r}"


def test_prompt_template_content_first_then_json_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #24: the prompt string passed to run_prompt starts with the raw
    Markdown template content and ends with the JSON evidence packet.
    This ordering keeps the cache-friendly prefix constant across scans."""
    scheduler, fake = _build_scheduler(tmp_path, monkeypatch)

    # Read the template so we can assert it comes first (byte-identical).
    template_path = (
        REPO_ROOT / "autofix_next" / "llm" / "prompts" / "unused_import_review.md"
    )
    assert template_path.is_file(), f"prompt template missing: {template_path}"
    template_text = template_path.read_text(encoding="utf-8")

    packet = _make_packet(prompt_prefix_hash="d" * 64)
    scheduler.schedule(packet)

    assert len(fake.calls) == 1
    prompt = fake.calls[0]["prompt"]
    assert isinstance(prompt, str)
    assert prompt.startswith(template_text), (
        "prompt must begin with the raw Markdown template content"
    )
    # The JSON packet is appended at the end. We can't predict exact byte
    # offsets but we can assert: (a) it's valid JSON, (b) contains packet keys.
    tail = prompt[len(template_text):].strip()
    # Pull out the last JSON-object substring — the packet is the last JSON.
    match = re.search(r"\{.*\}\s*$", tail, flags=re.DOTALL)
    assert match, f"prompt must end with a JSON object; tail={tail!r}"
    packet_json = json.loads(match.group(0))
    assert "schema_version" in packet_json
    assert packet_json["schema_version"] == "evidence_v1"
