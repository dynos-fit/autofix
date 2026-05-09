"""Freshness scoring (ARCH-016 AC-7..8).

Uses real ``LedgerRow`` instances rather than ``MagicMock``s so the
test can't pass against a fictional row schema. (A prior version of
this file mocked ``last_scanned_at``, an attribute that doesn't exist
on ``LedgerRow`` — masked a real production bug for months.)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from autofix.crawl.ledger import LedgerRow


def _mk_row(*, last_commit_sha: str, ts: str) -> LedgerRow:
    """Build a real LedgerRow with the fields that affect freshness."""
    return LedgerRow(
        ts=ts,
        bundle_fingerprint="fp",
        seed_path="seed.py",
        file_paths=("seed.py",),
        analyzer="cheap",
        last_commit_sha=last_commit_sha,
        last_finding_count=0,
        cache_hit=False,
        event_id="evt",
    )


def test_commit_sha_drift_returns_one() -> None:
    from autofix.crawl.score import file_freshness

    row = _mk_row(last_commit_sha="abc123", ts="2026-05-08T00:00:00Z")
    score = file_freshness(row, current_commit_sha="def456", now=None)
    assert score == 1.0


def test_no_drift_age_zero_returns_zero() -> None:
    from autofix.crawl.score import file_freshness

    now = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
    row = _mk_row(
        last_commit_sha="abc123",
        ts=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    score = file_freshness(row, current_commit_sha="abc123", now=now)
    assert score == 0.0


def test_no_drift_half_horizon_returns_half() -> None:
    """At 12h elapsed (half the 24h horizon), freshness = 0.5."""
    from autofix.crawl.score import file_freshness

    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    earlier = now - timedelta(hours=12)
    row = _mk_row(
        last_commit_sha="abc123",
        ts=earlier.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    score = file_freshness(row, current_commit_sha="abc123", now=now)
    assert abs(score - 0.5) < 0.01


def test_no_drift_clamps_at_one() -> None:
    """After 24h+ elapsed without commit drift, freshness clamps at 1.0."""
    from autofix.crawl.score import file_freshness

    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    earlier = now - timedelta(hours=72)
    row = _mk_row(
        last_commit_sha="abc123",
        ts=earlier.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    score = file_freshness(row, current_commit_sha="abc123", now=now)
    assert score == 1.0


def test_bundle_freshness_takes_max_across_files() -> None:
    """Bundle freshness = max(file_freshness for f in bundle.files)."""
    from autofix.crawl.score import bundle_freshness

    bundle = MagicMock()
    bundle.file_paths = (Path("file_a.py"), Path("file_b.py"))

    ledger = MagicMock()

    # file_a hasn't drifted (low freshness); file_b HAS drifted (1.0).
    def _latest(path: Path, analyzer: str | None) -> LedgerRow:
        if "b" in str(path):
            return _mk_row(last_commit_sha="different", ts="2026-05-07T00:00:00Z")
        return _mk_row(last_commit_sha="current", ts="2026-05-08T00:00:00Z")

    ledger.latest_for.side_effect = _latest

    score = bundle_freshness(bundle, ledger, current_commit_sha="current")
    assert score == 1.0


def test_bundle_freshness_unseen_file_is_one() -> None:
    """A file that has never been in the ledger is maximally stale (1.0)."""
    from autofix.crawl.score import bundle_freshness

    bundle = MagicMock()
    bundle.file_paths = (MagicMock(name="never_seen"),)

    ledger = MagicMock()
    ledger.latest_for.return_value = None  # no row for this file/analyzer

    score = bundle_freshness(bundle, ledger, current_commit_sha="any")
    assert score == 1.0
