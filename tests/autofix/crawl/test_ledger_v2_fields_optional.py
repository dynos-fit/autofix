"""Tests for LedgerRow v2 optional fields — covers ACs 15, 16.

ACs covered:
- AC 15: LedgerRow gains 4 new optional fields appended after the existing 9;
         from_dict uses .get() with None defaults; to_jsonl_line filters None values
- AC 16:
    (a) JSONL line without new fields round-trips through from_dict without error
    (b) LedgerRow with all 4 new fields set serializes and deserializes back identically
    (c) replay_from_disk on mix of old-format and new-format rows reads all without skipping
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_OLD_ROW_DICT = {
    "ts": "2026-05-08T00:00:00Z",
    "bundle_fingerprint": "a" * 64,
    "seed_path": "mymodule.py",
    "file_paths": ["mymodule.py", "utils.py"],
    "analyzer": "cheap",
    "last_commit_sha": "deadbeef",
    "last_finding_count": 3,
    "cache_hit": False,
    "event_id": "evt_abc123",
}

_NEW_ROW_DICT = {
    **_OLD_ROW_DICT,
    "bundle_fingerprint": "b" * 64,
    "scan_count_for_seed": 5,
    "imported_by_count_at_scan": 12,
    "bundle_size_bytes": 10240,
    "budget_hit_reason": "files",
}


# ---------------------------------------------------------------------------
# AC 15 — LedgerRow has the four new optional fields
# ---------------------------------------------------------------------------

def test_ledger_row_has_scan_count_for_seed() -> None:
    """LedgerRow must have scan_count_for_seed field with None default."""
    from autofix.crawl.ledger import LedgerRow
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(LedgerRow)}
    assert "scan_count_for_seed" in fields, "LedgerRow missing scan_count_for_seed"
    assert fields["scan_count_for_seed"].default is None


def test_ledger_row_has_imported_by_count_at_scan() -> None:
    """LedgerRow must have imported_by_count_at_scan field with None default."""
    from autofix.crawl.ledger import LedgerRow
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(LedgerRow)}
    assert "imported_by_count_at_scan" in fields
    assert fields["imported_by_count_at_scan"].default is None


def test_ledger_row_has_bundle_size_bytes() -> None:
    """LedgerRow must have bundle_size_bytes field with None default."""
    from autofix.crawl.ledger import LedgerRow
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(LedgerRow)}
    assert "bundle_size_bytes" in fields
    assert fields["bundle_size_bytes"].default is None


def test_ledger_row_has_budget_hit_reason() -> None:
    """LedgerRow must have budget_hit_reason field with None default."""
    from autofix.crawl.ledger import LedgerRow
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(LedgerRow)}
    assert "budget_hit_reason" in fields
    assert fields["budget_hit_reason"].default is None


def test_ledger_row_new_fields_come_after_required_fields() -> None:
    """New optional fields must be appended after the 9 existing required fields."""
    from autofix.crawl.ledger import LedgerRow
    import dataclasses

    field_names = [f.name for f in dataclasses.fields(LedgerRow)]
    required = ["ts", "bundle_fingerprint", "seed_path", "file_paths",
                "analyzer", "last_commit_sha", "last_finding_count", "cache_hit", "event_id"]
    optional = ["scan_count_for_seed", "imported_by_count_at_scan",
                "bundle_size_bytes", "budget_hit_reason"]

    # All required fields appear first
    for i, req in enumerate(required):
        assert field_names[i] == req, (
            f"Field ordering broken: expected {req!r} at index {i}, "
            f"got {field_names[i]!r}"
        )

    # Optional fields appear after required fields
    for opt in optional:
        idx = field_names.index(opt)
        assert idx >= len(required), (
            f"Optional field {opt!r} must come after required fields"
        )


def test_ledger_row_constructs_with_defaults() -> None:
    """LedgerRow can be constructed without the new optional fields."""
    from autofix.crawl.ledger import LedgerRow

    row = LedgerRow(
        ts="2026-05-08T00:00:00Z",
        bundle_fingerprint="a" * 64,
        seed_path="source.py",
        file_paths=("source.py",),
        analyzer="cheap",
        last_commit_sha="abc",
        last_finding_count=0,
        cache_hit=False,
        event_id="evt_1",
    )
    assert row.scan_count_for_seed is None
    assert row.imported_by_count_at_scan is None
    assert row.bundle_size_bytes is None
    assert row.budget_hit_reason is None


# ---------------------------------------------------------------------------
# AC 16a — Old-format JSONL round-trip through from_dict without error
# ---------------------------------------------------------------------------

def test_old_format_from_dict_no_error() -> None:
    """AC 16a: JSONL line without the 4 new fields parses without error."""
    from autofix.crawl.ledger import LedgerRow

    row = LedgerRow.from_dict(_OLD_ROW_DICT)
    assert row.ts == _OLD_ROW_DICT["ts"]
    assert row.scan_count_for_seed is None
    assert row.imported_by_count_at_scan is None
    assert row.bundle_size_bytes is None
    assert row.budget_hit_reason is None


def test_old_format_file_paths_preserved() -> None:
    """AC 16a: file_paths tuple is reconstructed from list."""
    from autofix.crawl.ledger import LedgerRow

    row = LedgerRow.from_dict(_OLD_ROW_DICT)
    assert row.file_paths == tuple(_OLD_ROW_DICT["file_paths"])


# ---------------------------------------------------------------------------
# AC 15 — to_jsonl_line filters None values
# ---------------------------------------------------------------------------

def test_to_jsonl_line_excludes_none_fields() -> None:
    """AC 15: to_jsonl_line filters out keys whose value is None."""
    from autofix.crawl.ledger import LedgerRow

    row = LedgerRow(
        ts="2026-05-08T00:00:00Z",
        bundle_fingerprint="a" * 64,
        seed_path="source.py",
        file_paths=("source.py",),
        analyzer="cheap",
        last_commit_sha="abc",
        last_finding_count=0,
        cache_hit=False,
        event_id="evt_1",
        # All new fields default to None
    )
    line = row.to_jsonl_line()
    d = json.loads(line.strip())

    assert "scan_count_for_seed" not in d
    assert "imported_by_count_at_scan" not in d
    assert "bundle_size_bytes" not in d
    assert "budget_hit_reason" not in d


def test_to_jsonl_line_includes_non_none_new_fields() -> None:
    """AC 15: to_jsonl_line includes new fields when they are non-None."""
    from autofix.crawl.ledger import LedgerRow

    row = LedgerRow(
        ts="2026-05-08T00:00:00Z",
        bundle_fingerprint="b" * 64,
        seed_path="source.py",
        file_paths=("source.py",),
        analyzer="cheap",
        last_commit_sha="abc",
        last_finding_count=2,
        cache_hit=False,
        event_id="evt_2",
        scan_count_for_seed=3,
        imported_by_count_at_scan=7,
        bundle_size_bytes=2048,
        budget_hit_reason="files",
    )
    line = row.to_jsonl_line()
    d = json.loads(line.strip())

    assert d["scan_count_for_seed"] == 3
    assert d["imported_by_count_at_scan"] == 7
    assert d["bundle_size_bytes"] == 2048
    assert d["budget_hit_reason"] == "files"


def test_to_jsonl_line_old_format_not_polluted() -> None:
    """AC 15: rows without new fields do NOT get null keys in JSONL output."""
    from autofix.crawl.ledger import LedgerRow

    row = LedgerRow.from_dict(_OLD_ROW_DICT)
    line = row.to_jsonl_line()
    # Verify "null" does not appear in the serialized line for new field keys
    d = json.loads(line.strip())
    for key in ["scan_count_for_seed", "imported_by_count_at_scan",
                "bundle_size_bytes", "budget_hit_reason"]:
        assert key not in d, f"Null key {key!r} must not appear in old-format JSONL"


# ---------------------------------------------------------------------------
# AC 16b — New-format row round-trips with identical values
# ---------------------------------------------------------------------------

def test_new_format_round_trip_identical_values() -> None:
    """AC 16b: LedgerRow with all 4 new fields set serializes and deserializes identically."""
    from autofix.crawl.ledger import LedgerRow

    original = LedgerRow(
        ts="2026-05-08T12:00:00Z",
        bundle_fingerprint="c" * 64,
        seed_path="core.py",
        file_paths=("core.py", "utils.py"),
        analyzer="llm:security",
        last_commit_sha="cafebabe",
        last_finding_count=5,
        cache_hit=True,
        event_id="evt_roundtrip",
        scan_count_for_seed=10,
        imported_by_count_at_scan=25,
        bundle_size_bytes=8192,
        budget_hit_reason="bytes",
    )

    jsonl_line = original.to_jsonl_line()
    d = json.loads(jsonl_line.strip())
    restored = LedgerRow.from_dict(d)

    assert restored.ts == original.ts
    assert restored.bundle_fingerprint == original.bundle_fingerprint
    assert restored.seed_path == original.seed_path
    assert restored.file_paths == original.file_paths
    assert restored.analyzer == original.analyzer
    assert restored.last_commit_sha == original.last_commit_sha
    assert restored.last_finding_count == original.last_finding_count
    assert restored.cache_hit == original.cache_hit
    assert restored.event_id == original.event_id
    assert restored.scan_count_for_seed == original.scan_count_for_seed
    assert restored.imported_by_count_at_scan == original.imported_by_count_at_scan
    assert restored.bundle_size_bytes == original.bundle_size_bytes
    assert restored.budget_hit_reason == original.budget_hit_reason


def test_new_format_from_dict_extracts_all_new_fields() -> None:
    """AC 16b: from_dict extracts all 4 new fields when present."""
    from autofix.crawl.ledger import LedgerRow

    row = LedgerRow.from_dict(_NEW_ROW_DICT)
    assert row.scan_count_for_seed == 5
    assert row.imported_by_count_at_scan == 12
    assert row.bundle_size_bytes == 10240
    assert row.budget_hit_reason == "files"


# ---------------------------------------------------------------------------
# AC 16c — replay_from_disk reads both old and new format rows
# ---------------------------------------------------------------------------

def test_replay_from_disk_mixed_old_and_new_format(tmp_path: Path) -> None:
    """AC 16c: replay_from_disk on mix of old+new format rows reads all without skipping."""
    from autofix.crawl.ledger import Ledger, LedgerRow

    # Create .autofix directory
    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir()
    ledger_file = autofix_dir / "crawl-ledger.jsonl"

    # Old-format row
    old_row = LedgerRow.from_dict(_OLD_ROW_DICT)
    # New-format row
    new_row = LedgerRow.from_dict(_NEW_ROW_DICT)

    with ledger_file.open("w", encoding="utf-8") as f:
        f.write(old_row.to_jsonl_line())
        f.write(new_row.to_jsonl_line())

    ledger = Ledger(root=tmp_path)
    ledger.replay_from_disk()

    rows = list(ledger)
    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"

    # Old-format row should have None for new fields
    old_parsed = rows[0]
    assert old_parsed.scan_count_for_seed is None
    assert old_parsed.budget_hit_reason is None

    # New-format row should have correct values
    new_parsed = rows[1]
    assert new_parsed.scan_count_for_seed == 5
    assert new_parsed.budget_hit_reason == "files"


def test_replay_from_disk_all_old_format_rows(tmp_path: Path) -> None:
    """AC 16c: replay_from_disk on ledger with only old-format rows reads all rows."""
    from autofix.crawl.ledger import Ledger, LedgerRow

    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir()
    ledger_file = autofix_dir / "crawl-ledger.jsonl"

    # Write 3 old-format rows
    with ledger_file.open("w", encoding="utf-8") as f:
        for i in range(3):
            d = {**_OLD_ROW_DICT, "bundle_fingerprint": f"{i:064d}", "event_id": f"evt_{i}"}
            row = LedgerRow.from_dict(d)
            f.write(row.to_jsonl_line())

    ledger = Ledger(root=tmp_path)
    ledger.replay_from_disk()

    rows = list(ledger)
    assert len(rows) == 3


def test_replay_from_disk_all_new_format_rows(tmp_path: Path) -> None:
    """AC 16c: replay_from_disk on ledger with only new-format rows reads all rows."""
    from autofix.crawl.ledger import Ledger, LedgerRow

    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir()
    ledger_file = autofix_dir / "crawl-ledger.jsonl"

    with ledger_file.open("w", encoding="utf-8") as f:
        for i in range(3):
            d = {
                **_NEW_ROW_DICT,
                "bundle_fingerprint": f"{i:064d}",
                "event_id": f"evt_{i}",
                "scan_count_for_seed": i + 1,
            }
            row = LedgerRow.from_dict(d)
            f.write(row.to_jsonl_line())

    ledger = Ledger(root=tmp_path)
    ledger.replay_from_disk()

    rows = list(ledger)
    assert len(rows) == 3
    for i, row in enumerate(rows):
        assert row.scan_count_for_seed == i + 1


# ---------------------------------------------------------------------------
# AC 15 — budget_hit_reason values must come from crawl_constants
# (structural test — verifies the constants exist)
# ---------------------------------------------------------------------------

def test_budget_hit_reason_constants_exist() -> None:
    """AC 15: BUDGET_HIT_REASON_* constants must exist in crawl_constants."""
    from autofix.crawl.crawl_constants import (
        BUDGET_HIT_REASON_FILES,
        BUDGET_HIT_REASON_BYTES,
        BUDGET_HIT_REASON_HOPS,
        BUDGET_HIT_REASON_NONE,
    )
    assert BUDGET_HIT_REASON_FILES == "files"
    assert BUDGET_HIT_REASON_BYTES == "bytes"
    assert BUDGET_HIT_REASON_HOPS == "hops"
    assert BUDGET_HIT_REASON_NONE == "none"


def test_budget_hit_reason_values_round_trip() -> None:
    """All BUDGET_HIT_REASON_* values round-trip through LedgerRow."""
    from autofix.crawl.ledger import LedgerRow
    from autofix.crawl.crawl_constants import (
        BUDGET_HIT_REASON_FILES,
        BUDGET_HIT_REASON_BYTES,
        BUDGET_HIT_REASON_HOPS,
        BUDGET_HIT_REASON_NONE,
    )

    for reason in [BUDGET_HIT_REASON_FILES, BUDGET_HIT_REASON_BYTES,
                   BUDGET_HIT_REASON_HOPS, BUDGET_HIT_REASON_NONE]:
        row = LedgerRow(
            ts="2026-05-08T00:00:00Z",
            bundle_fingerprint="d" * 64,
            seed_path="x.py",
            file_paths=("x.py",),
            analyzer="cheap",
            last_commit_sha="abc",
            last_finding_count=0,
            cache_hit=False,
            event_id="evt_x",
            budget_hit_reason=reason,
        )
        line = row.to_jsonl_line()
        d = json.loads(line.strip())
        restored = LedgerRow.from_dict(d)
        assert restored.budget_hit_reason == reason
