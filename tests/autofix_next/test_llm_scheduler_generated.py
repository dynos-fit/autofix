"""Tests for the generated/vendor-code gate inside Scheduler._process_one.

Covers acceptance criteria 7, 8, 9, 10, 11, 12, 13 from task-20260417-008:

  AC 7  — generated-gate is a private helper; glob test first, marker
          scan on glob miss reads first 2048 bytes.
  AC 8  — default glob list (policy override appended).
  AC 9  — default marker list, case-insensitive substring.
  AC 10 — binary NUL in first 2048 bytes → treat as generated.
  AC 11 — FileNotFoundError / IsADirectoryError / PermissionError
          during read → fail open (proceed as not-generated).
  AC 12 — ``generated_disabled=true`` skips the gate wholesale.
  AC 13 — generated path emits exactly one ``skipped_generated`` event,
          no ``run_prompt`` invocation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


class _CountingFake:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, prompt, **kwargs):
        from autofix.llm_backend import LLMResult

        self.calls.append({"prompt": prompt, "kwargs": dict(kwargs)})
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

    def _fake(root, event_type, scan_event, **kwargs):
        captured.append({"event": event_type, "payload": dict(scan_event)})

    monkeypatch.setattr(
        "autofix_next.telemetry.events_log.append_event", _fake
    )
    return captured


def _build(tmp_path: Path, monkeypatch, policy: dict | None = None):
    from autofix_next.llm.scheduler import Scheduler

    fake = _CountingFake()
    monkeypatch.setattr("autofix.llm_backend.run_prompt", fake)
    events = _install_events(monkeypatch)
    _write_policy(tmp_path, policy or {})
    monkeypatch.setenv("AUTOFIX_NEXT_LLM_CACHE_DIR", str(tmp_path / "cache"))
    scheduler = Scheduler(root=tmp_path)
    return scheduler, fake, events


# ---------------------------------------------------------------------------
# AC 8 — default globs match.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relpath",
    [
        "vendor/lib/x.py",
        "node_modules/pkg/index.js",
        "dist/bundle.py",
        "build/output.py",
        ".generated/models.py",
        "sub/.generated/x.py",
        "static/main.min.js",
        "style.min.css",
        "pb/service.pb.go",
        "pb/service_pb2.py",
        "pb/service_pb2.pyi",
        "__pycache__/x.py",
        "subdir/__pycache__/y.py",
    ],
)
def test_default_globs_match_common_generated_paths(tmp_path, monkeypatch, relpath):
    """AC 8: each default glob triggers ``skipped_generated`` for its path."""
    # Place a benign (non-generated) byte payload at the path so a marker
    # scan, if it ran, would fail to flag it. The glob check must short-
    # circuit BEFORE marker scan.
    target = tmp_path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("clean content\n", encoding="utf-8")

    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(
        _make_packet(primary_symbol=f"{relpath}::symbol")
    )

    assert decision.decision == "skipped_generated", (
        f"default glob {relpath!r} must map to skipped_generated"
    )
    assert fake.calls == [], "glob-gated path must not invoke run_prompt"
    names = [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]
    assert names.count("skipped_generated") == 1, (
        f"exactly one skipped_generated event expected: {names!r}"
    )


def test_policy_globs_are_appended_to_defaults(tmp_path, monkeypatch):
    """AC 8: caller-provided globs are appended to the default list."""
    target = tmp_path / "proto" / "service.proto.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("clean content\n", encoding="utf-8")

    scheduler, fake, events = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"generated_globs": ["**/*.proto.py"]}},
    )

    decision = scheduler.schedule(
        _make_packet(primary_symbol="proto/service.proto.py::X")
    )

    assert decision.decision == "skipped_generated"
    assert fake.calls == []


def test_policy_globs_do_not_displace_defaults(tmp_path, monkeypatch):
    """AC 8: policy-provided globs are APPENDED, not replacing defaults.

    A default glob (e.g. ``**/vendor/**``) continues to trigger even when
    the policy supplies an unrelated extra glob.
    """
    target = tmp_path / "vendor" / "lib" / "x.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("clean content\n", encoding="utf-8")

    scheduler, fake, _ = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"generated_globs": ["**/*.proto.py"]}},
    )

    decision = scheduler.schedule(
        _make_packet(primary_symbol="vendor/lib/x.py::X")
    )
    assert decision.decision == "skipped_generated", (
        "default vendor glob must still trigger when policy adds extras"
    )
    assert fake.calls == []


# ---------------------------------------------------------------------------
# AC 9 — default markers trigger on case-insensitive substring.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content_head",
    [
        "# DO NOT EDIT\nimport x\n",
        "# do not edit this file\nimport x\n",
        "// Code generated by protoc. DO NOT EDIT.\nimport x\n",
        "# @generated from schema.proto\nimport x\n",
        "// AUTOGENERATED FILE\nimport x\n",
        "# autogenerated\nimport x\n",
    ],
)
def test_default_markers_trigger_generated_skip(tmp_path, monkeypatch, content_head):
    """AC 9: each default marker appears as a case-insensitive substring."""
    target = tmp_path / "clean.py"
    target.write_text(content_head, encoding="utf-8")

    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(
        _make_packet(primary_symbol="clean.py::os")
    )

    assert decision.decision == "skipped_generated", (
        f"marker-bearing file must be flagged as generated: head={content_head!r}"
    )
    assert fake.calls == []
    names = [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]
    assert names.count("skipped_generated") == 1


def test_policy_markers_are_appended(tmp_path, monkeypatch):
    """AC 9: custom markers are appended to the default list."""
    target = tmp_path / "clean.py"
    target.write_text("// CUSTOM_AUTOGEN_SENTINEL\nimport x\n", encoding="utf-8")

    scheduler, fake, _ = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"generated_markers": ["CUSTOM_AUTOGEN_SENTINEL"]}},
    )
    decision = scheduler.schedule(
        _make_packet(primary_symbol="clean.py::os")
    )

    assert decision.decision == "skipped_generated"
    assert fake.calls == []


def test_clean_file_is_not_flagged_as_generated(tmp_path, monkeypatch):
    """AC 7/13 negative: no glob, no marker → promoted, not skipped."""
    target = tmp_path / "clean.py"
    target.write_text("import os\nprint(os.getcwd())\n", encoding="utf-8")

    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet(primary_symbol="clean.py::os"))

    names = [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]
    assert "skipped_generated" not in names, (
        f"clean file must not be skipped as generated: names={names!r}"
    )
    assert len(fake.calls) == 1
    assert decision.decision in {"promoted", "promoted_default_tier"}


# ---------------------------------------------------------------------------
# AC 10 — NUL byte in first 2048 bytes.
# ---------------------------------------------------------------------------


def test_binary_nul_byte_flags_as_generated(tmp_path, monkeypatch):
    """AC 10: NUL byte in first 2 KB → treat file as generated."""
    target = tmp_path / "bin.dat"
    target.write_bytes(b"import os\n\x00" + b"A" * 100)

    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet(primary_symbol="bin.dat::os"))

    assert decision.decision == "skipped_generated", (
        f"NUL in first 2 KB must flag as generated: {decision.decision!r}"
    )
    assert fake.calls == []
    names = [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]
    assert "skipped_generated" in names


def test_nul_byte_beyond_first_2048_bytes_is_ignored(tmp_path, monkeypatch):
    """AC 10 edge: NUL past the 2 KB window does NOT flag."""
    target = tmp_path / "bin.dat"
    target.write_bytes(b"A" * 3000 + b"\x00" + b"A" * 100)

    scheduler, fake, _ = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet(primary_symbol="bin.dat::os"))

    assert decision.decision != "skipped_generated", (
        "NUL beyond 2048-byte window must not flag as generated"
    )
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# AC 11 — fail-open on read errors.
# ---------------------------------------------------------------------------


def test_missing_file_fails_open(tmp_path, monkeypatch):
    """AC 11: FileNotFoundError during marker read → treat as not generated."""
    # We intentionally do NOT create the file at path ``missing.py``.
    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet(primary_symbol="missing.py::os"))

    names = [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]
    assert "skipped_generated" not in names, (
        f"missing file must fail open: names={names!r}"
    )
    assert decision.decision in {"promoted", "promoted_default_tier"}
    assert len(fake.calls) == 1


def test_directory_at_primary_symbol_fails_open(tmp_path, monkeypatch):
    """AC 11: IsADirectoryError during marker read → treat as not generated."""
    (tmp_path / "some_dir").mkdir(parents=True, exist_ok=True)

    scheduler, fake, events = _build(tmp_path, monkeypatch)
    decision = scheduler.schedule(_make_packet(primary_symbol="some_dir::os"))

    names = [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]
    assert "skipped_generated" not in names
    assert decision.decision in {"promoted", "promoted_default_tier"}
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# AC 12 — ``generated_disabled`` kill switch.
# ---------------------------------------------------------------------------


def test_generated_disabled_skips_gate_wholesale(tmp_path, monkeypatch):
    """AC 12: policy ``generated_disabled=true`` disables the gate."""
    # A path that WOULD match a default glob (vendor/**) but the gate is off.
    target = tmp_path / "vendor" / "lib" / "x.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("import os\n", encoding="utf-8")

    scheduler, fake, events = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"generated_disabled": True}},
    )
    decision = scheduler.schedule(
        _make_packet(primary_symbol="vendor/lib/x.py::os")
    )

    names = [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]
    assert "skipped_generated" not in names, (
        f"generated_disabled=true must suppress the gate: names={names!r}"
    )
    assert decision.decision in {"promoted", "promoted_default_tier"}
    assert len(fake.calls) == 1


def test_generated_disabled_also_skips_marker_scan(tmp_path, monkeypatch):
    """AC 12: disabled gate also skips the marker scan.

    A file whose head contains ``@generated`` is NOT flagged when the gate
    is disabled.
    """
    target = tmp_path / "clean.py"
    target.write_text("# @generated\nimport os\n", encoding="utf-8")

    scheduler, fake, events = _build(
        tmp_path,
        monkeypatch,
        policy={"llm_tiered": {"generated_disabled": True}},
    )
    scheduler.schedule(_make_packet(primary_symbol="clean.py::os"))

    names = [e["payload"].get("decision") for e in events if e["event"] == "LLMCallGated"]
    assert "skipped_generated" not in names
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# AC 13 — generated path emits exactly one event, no run_prompt.
# ---------------------------------------------------------------------------


def test_generated_path_emits_exactly_one_event_no_run_prompt(tmp_path, monkeypatch):
    """AC 13: generated decision emits one ``skipped_generated`` event only."""
    target = tmp_path / "vendor" / "lib" / "x.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("import os\n", encoding="utf-8")

    scheduler, fake, events = _build(tmp_path, monkeypatch)
    scheduler.schedule(_make_packet(primary_symbol="vendor/lib/x.py::os"))

    gated = [e for e in events if e["event"] == "LLMCallGated"]
    assert len(gated) == 1, (
        f"exactly one LLMCallGated event expected on the generated path: {gated!r}"
    )
    assert gated[0]["payload"]["decision"] == "skipped_generated"
    assert fake.calls == [], "generated path must not invoke run_prompt"
