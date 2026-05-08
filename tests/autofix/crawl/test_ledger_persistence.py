"""Ledger persistence (ARCH-016 AC-10..12)."""
from __future__ import annotations

from pathlib import Path

import pytest


def _row(**overrides):
    """Build a default LedgerRow with overridable fields."""
    from autofix.crawl.ledger import LedgerRow

    defaults = {
        "ts": "2026-05-08T00:00:00Z",
        "bundle_fingerprint": "a" * 64,
        "seed_path": "autofix/cli/run_command.py",
        "file_paths": ("autofix/cli/run_command.py",),
        "analyzer": "llm:security",
        "last_commit_sha": "abc123",
        "last_finding_count": 0,
        "cache_hit": False,
        "event_id": "evt_test_1",
    }
    defaults.update(overrides)
    return LedgerRow(**defaults)


def test_ledger_row_is_frozen() -> None:
    import dataclasses
    from autofix.crawl.ledger import LedgerRow

    assert dataclasses.is_dataclass(LedgerRow)
    r = _row()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.ts = "2026-05-09T00:00:00Z"  # type: ignore[misc]


def test_record_appends_jsonl_line(tmp_path: Path) -> None:
    """``record`` writes one JSONL line to the ledger file."""
    from autofix.crawl.ledger import Ledger

    ledger = Ledger(root=tmp_path)
    ledger.record(_row(seed_path="a.py"))
    ledger.record(_row(seed_path="b.py"))

    log = tmp_path / ".autofix" / "crawl-ledger.jsonl"
    lines = log.read_text().splitlines()
    assert len(lines) == 2

    import json
    rows = [json.loads(line) for line in lines]
    assert rows[0]["seed_path"] == "a.py"
    assert rows[1]["seed_path"] == "b.py"


def test_replay_round_trip(tmp_path: Path) -> None:
    """Append rows, replay from disk, get back the same rows."""
    from autofix.crawl.ledger import Ledger, LedgerRow

    writer = Ledger(root=tmp_path)
    writer.record(_row(seed_path="a.py", analyzer="llm:security"))
    writer.record(_row(seed_path="b.py", analyzer="llm:dead-code"))
    writer.record(_row(seed_path="a.py", analyzer="llm:dead-code"))

    reader = Ledger(root=tmp_path)
    reader.replay_from_disk()

    rows = list(reader)
    assert len(rows) == 3
    assert all(isinstance(r, LedgerRow) for r in rows)


def test_latest_for_returns_most_recent_row(tmp_path: Path) -> None:
    """``latest_for(fp, analyzer)`` returns the most-recently-recorded row."""
    from autofix.crawl.ledger import Ledger

    ledger = Ledger(root=tmp_path)
    fp = "f" * 64
    ledger.record(_row(
        bundle_fingerprint=fp, analyzer="llm:security",
        ts="2026-05-08T00:00:00Z", last_finding_count=2,
    ))
    ledger.record(_row(
        bundle_fingerprint=fp, analyzer="llm:security",
        ts="2026-05-08T01:00:00Z", last_finding_count=5,
    ))

    latest = ledger.latest_for(fp, "llm:security")
    assert latest is not None
    assert latest.last_finding_count == 5


def test_latest_for_missing_returns_none(tmp_path: Path) -> None:
    from autofix.crawl.ledger import Ledger

    ledger = Ledger(root=tmp_path)
    assert ledger.latest_for("absent" * 16, "llm:security") is None


def test_bundle_appearance_count_in_window(tmp_path: Path) -> None:
    """Counts how many bundles a file appeared in within a time window."""
    from autofix.crawl.ledger import Ledger

    ledger = Ledger(root=tmp_path)
    # File "hub.py" appears in 3 bundles within window, 1 outside.
    ledger.record(_row(
        bundle_fingerprint="0" * 64, ts="2026-05-08T00:00:00Z",
        file_paths=("hub.py", "a.py"),
    ))
    ledger.record(_row(
        bundle_fingerprint="1" * 64, ts="2026-05-08T01:00:00Z",
        file_paths=("hub.py", "b.py"),
    ))
    ledger.record(_row(
        bundle_fingerprint="2" * 64, ts="2026-05-08T02:00:00Z",
        file_paths=("hub.py", "c.py"),
    ))
    ledger.record(_row(
        bundle_fingerprint="3" * 64, ts="2026-05-06T00:00:00Z",
        file_paths=("hub.py", "d.py"),
    ))

    count = ledger.bundle_appearance_count_in_window(
        "hub.py",
        window_start="2026-05-07T23:59:00Z",
        now="2026-05-08T03:00:00Z",
    )
    assert count == 3

    # Different fingerprint per call ensures we count distinct bundles, not rows.
    # (Two records with the same fingerprint == one bundle, not two.)


def test_appearance_counts_distinct_bundles(tmp_path: Path) -> None:
    """Same fingerprint × multiple analyzers = ONE bundle for saturation."""
    from autofix.crawl.ledger import Ledger

    ledger = Ledger(root=tmp_path)
    fp = "z" * 64
    # Same bundle, two analyzers, two ledger rows — counts as 1 bundle.
    ledger.record(_row(
        bundle_fingerprint=fp, analyzer="llm:security",
        ts="2026-05-08T00:00:00Z", file_paths=("hub.py",),
    ))
    ledger.record(_row(
        bundle_fingerprint=fp, analyzer="llm:dead-code",
        ts="2026-05-08T00:00:01Z", file_paths=("hub.py",),
    ))

    count = ledger.bundle_appearance_count_in_window(
        "hub.py",
        window_start="2026-05-08T00:00:00Z",
        now="2026-05-08T01:00:00Z",
    )
    assert count == 1


def test_replay_skips_half_written_lines(tmp_path: Path) -> None:
    """A truncated/malformed line in the ledger is logged + skipped."""
    from autofix.crawl.ledger import Ledger

    log_dir = tmp_path / ".autofix"
    log_dir.mkdir()
    log = log_dir / "crawl-ledger.jsonl"
    log.write_text(
        '{"ts":"2026-05-08T00:00:00Z","bundle_fingerprint":"' + "a" * 64
        + '","seed_path":"a.py","file_paths":["a.py"],"analyzer":"cheap",'
        '"last_commit_sha":"abc","last_finding_count":0,"cache_hit":false,"event_id":"e1"}\n'
        '{not valid json\n'
        '{"ts":"2026-05-08T01:00:00Z","bundle_fingerprint":"' + "b" * 64
        + '","seed_path":"b.py","file_paths":["b.py"],"analyzer":"cheap",'
        '"last_commit_sha":"abc","last_finding_count":1,"cache_hit":false,"event_id":"e2"}\n'
    )

    ledger = Ledger(root=tmp_path)
    ledger.replay_from_disk()
    rows = list(ledger)
    assert len(rows) == 2
    seeds = [r.seed_path for r in rows]
    assert seeds == ["a.py", "b.py"]


def test_replay_missing_file_is_empty(tmp_path: Path) -> None:
    """Replay against a missing file produces an empty ledger, no error."""
    from autofix.crawl.ledger import Ledger

    ledger = Ledger(root=tmp_path)
    ledger.replay_from_disk()
    assert list(ledger) == []
