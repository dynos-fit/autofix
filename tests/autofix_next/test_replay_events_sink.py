"""AC 55 — Replay-mode redirects ``append_event`` to an in-memory sink.

Inside ``with replay_mode():``, :func:`append_event` must route rows to the
``_REPLAY_EVENTS_SINK`` list without touching ``<root>/.autofix/events.jsonl``.
This is the invariant that lets replay re-run the pipeline byte-identically
without corrupting the historical stream.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_replay_mode_redirects_writes_to_in_memory_sink(tmp_path: Path) -> None:
    """AC 55 (primary): inside ``replay_mode()``, ``append_event`` must not
    touch events.jsonl; rows must land on ``_REPLAY_EVENTS_SINK``."""
    from autofix_next.telemetry.events_log import append_event
    from autofix_next.telemetry.replay import _REPLAY_EVENTS_SINK, replay_mode

    # Seed events.jsonl with a legacy row and capture its bytes.
    events_dir = tmp_path / ".autofix"
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"
    seed = b'{"event":"legacy","at":"2026-04-01T00:00:00Z"}\n'
    events_path.write_bytes(seed)
    pre_bytes = events_path.read_bytes()
    pre_sha = _sha256(pre_bytes)

    payload = {
        "event_type": "ScanStarted",
        "repo_id": tmp_path.name,
        "commit_sha": None,
        "base_sha": None,
        "watcher_confidence": "diff-head1",
        "source": "cli",
    }

    with replay_mode():
        # AC 55: sink must be a list inside replay_mode.
        sink = _REPLAY_EVENTS_SINK.get()
        assert isinstance(sink, list), (
            f"sink must be a list inside replay_mode; got {type(sink)!r}"
        )
        assert sink == [], "sink must start empty on entry"

        returned_id = append_event(tmp_path, "ScanStarted", payload)
        # The writer returns a sentinel event id during replay rather than
        # writing to disk.
        assert isinstance(returned_id, str)

        sink_after = _REPLAY_EVENTS_SINK.get()
        assert isinstance(sink_after, list)
        assert len(sink_after) == 1, (
            f"sink must hold exactly the one row just appended; got "
            f"len={len(sink_after)!r}: {sink_after!r}"
        )
        row = sink_after[0]
        assert row["event_type"] == "ScanStarted"
        assert row["payload"] == payload
        # payload must be a copy, not an alias — else the caller can later
        # mutate the sink's row. Verify by mutating the original dict.
        payload["commit_sha"] = "poisoned"
        assert row["payload"]["commit_sha"] is None, (
            "sink must hold a *copy* of the payload, not an alias"
        )

    # AC 55 (invariant): events.jsonl bytes unchanged by the replay write.
    post_bytes = events_path.read_bytes()
    assert post_bytes == pre_bytes, (
        f"events.jsonl bytes changed during replay_mode; "
        f"pre sha={pre_sha}, post sha={_sha256(post_bytes)}"
    )


def test_append_event_writes_to_disk_outside_replay_mode(tmp_path: Path) -> None:
    """Negative control: with replay_mode NOT active, append_event writes to
    events.jsonl as normal. This makes the positive assertion meaningful:
    the sink redirect is a property of ``replay_mode``, not of the writer."""
    from autofix_next.telemetry.events_log import append_event
    from autofix_next.telemetry.replay import _REPLAY_EVENTS_SINK

    # Sanity: outside replay_mode the sink is None.
    assert _REPLAY_EVENTS_SINK.get() is None

    payload = {
        "event_type": "ScanStarted",
        "repo_id": tmp_path.name,
        "commit_sha": None,
        "base_sha": None,
        "watcher_confidence": "diff-head1",
        "source": "cli",
    }
    append_event(tmp_path, "ScanStarted", payload)
    events_path = tmp_path / ".autofix" / "events.jsonl"
    assert events_path.is_file(), "events.jsonl must be created on disk"
    text = events_path.read_text(encoding="utf-8")
    assert text.strip(), "events.jsonl must have at least one row"


def test_replay_mode_exit_restores_sink_to_none(tmp_path: Path) -> None:
    """Exiting replay_mode must pop the sink back to None so a subsequent
    append_event write lands on disk (regression guard for a dangling
    ContextVar token)."""
    from autofix_next.telemetry.events_log import append_event
    from autofix_next.telemetry.replay import _REPLAY_EVENTS_SINK, replay_mode

    with replay_mode():
        assert _REPLAY_EVENTS_SINK.get() == []
    assert _REPLAY_EVENTS_SINK.get() is None, (
        "sink must reset to None after replay_mode exits"
    )

    # Post-exit write must land on disk.
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
    assert events_path.read_bytes().strip(), (
        "events.jsonl must be written after replay_mode exits"
    )
