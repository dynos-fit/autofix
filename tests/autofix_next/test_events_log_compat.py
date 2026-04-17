"""Tests for autofix_next.telemetry.events_log.append_event.

Covers:
  AC #11 — pre-existing byte prefix of .autofix/events.jsonl is preserved.
  AC #12 — every new row has top-level keys {event, at, scan_event}, parses
           as JSON, is UTF-8, \\n-terminated.
  AC #23 — legacy rows (from autofix.runtime.dynos.log_event) and new rows
           coexist in the same file; both parse via json.loads.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


def test_append_preserves_existing_file_prefix_bytes(tmp_path: Path) -> None:
    """AC #11: the byte prefix of .autofix/events.jsonl that existed before
    append_event() was called is preserved byte-identical afterwards."""
    from autofix_next.telemetry.events_log import append_event

    events_dir = tmp_path / ".autofix"
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"
    pre_existing_lines = [
        '{"event": "legacy_scan", "at": "2026-04-01T00:00:00Z", "note": "hi"}\n',
        '{"event": "legacy_scan_done", "at": "2026-04-01T00:00:05Z"}\n',
    ]
    events_path.write_text("".join(pre_existing_lines), encoding="utf-8")
    pre_bytes = events_path.read_bytes()
    pre_hash = hashlib.sha256(pre_bytes).hexdigest()

    append_event(
        tmp_path,
        "ScanStarted",
        {
            "event_type": "ScanStarted",
            "repo_id": tmp_path.name,
            "commit_sha": None,
            "base_sha": None,
            "watcher_confidence": "diff-head1",
            "source": "cli",
        },
    )

    post_bytes = events_path.read_bytes()
    # The file grew; the first len(pre_bytes) bytes must be byte-identical.
    assert post_bytes.startswith(pre_bytes), (
        "append_event must not touch the existing prefix"
    )
    assert hashlib.sha256(post_bytes[: len(pre_bytes)]).hexdigest() == pre_hash


def test_new_row_has_event_at_scan_event_keys(tmp_path: Path) -> None:
    """AC #12: new row is a single-line UTF-8 JSON object with top-level
    keys 'event' (string), 'at' (ISO-8601 UTC string), plus a 'scan_event'
    payload object, and terminates with '\\n'."""
    from autofix_next.telemetry.events_log import append_event

    # append_event must create .autofix/ if it doesn't yet exist.
    append_event(
        tmp_path,
        "ScanStarted",
        {
            "event_type": "ScanStarted",
            "repo_id": tmp_path.name,
            "commit_sha": None,
            "base_sha": None,
            "watcher_confidence": "diff-head1",
            "source": "cli",
        },
    )
    events_path = tmp_path / ".autofix" / "events.jsonl"
    assert events_path.is_file()
    raw = events_path.read_bytes()
    # UTF-8-decodable and newline-terminated.
    text = raw.decode("utf-8")
    assert text.endswith("\n"), f"events.jsonl must be newline-terminated: {text!r}"
    # One line per append.
    line = text.splitlines()[-1]
    row = json.loads(line)
    assert isinstance(row["event"], str) and row["event"] == "ScanStarted"
    assert isinstance(row["at"], str) and row["at"].endswith("Z")
    assert "scan_event" in row and isinstance(row["scan_event"], dict)


def test_legacy_and_new_rows_coexist(tmp_path: Path) -> None:
    """AC #23: writing a legacy row via autofix.runtime.dynos.log_event and a
    new row via autofix_next.telemetry.events_log.append_event produces a
    file where both lines parse as JSON."""
    from autofix.runtime.dynos import log_event as legacy_log_event
    from autofix_next.telemetry.events_log import append_event

    # Legacy writer first.
    legacy_log_event(tmp_path, "scan_started", foo="bar")

    # New writer second.
    append_event(
        tmp_path,
        "ScanStarted",
        {
            "event_type": "ScanStarted",
            "repo_id": tmp_path.name,
            "commit_sha": None,
            "base_sha": None,
            "watcher_confidence": "diff-head1",
            "source": "cli",
        },
    )

    events_path = tmp_path / ".autofix" / "events.jsonl"
    assert events_path.is_file()
    lines = [
        line for line in events_path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert len(lines) >= 2

    legacy_line = json.loads(lines[0])
    new_line = json.loads(lines[-1])

    # Legacy row retains its original snake_case event name and keys.
    assert legacy_line["event"] == "scan_started"
    assert legacy_line.get("foo") == "bar"
    assert "at" in legacy_line

    # New row carries the new envelope keys.
    assert new_line["event"] == "ScanStarted"
    assert "at" in new_line
    assert "scan_event" in new_line
