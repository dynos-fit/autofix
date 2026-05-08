"""Freshness scoring (ARCH-016 AC-7..8)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock



def _mk_row(*, last_commit_sha: str, last_scanned_at: str) -> MagicMock:
    r = MagicMock()
    r.last_commit_sha = last_commit_sha
    r.last_scanned_at = last_scanned_at
    return r


def test_commit_sha_drift_returns_one() -> None:
    from autofix.crawl.score import file_freshness

    row = _mk_row(
        last_commit_sha="abc123",
        last_scanned_at="2026-05-08T00:00:00Z",
    )
    score = file_freshness(row, current_commit_sha="def456", now=None)
    assert score == 1.0


def test_no_drift_age_zero_returns_zero() -> None:
    from autofix.crawl.score import file_freshness

    now = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
    row = _mk_row(
        last_commit_sha="abc123",
        last_scanned_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        last_scanned_at=earlier.strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        last_scanned_at=earlier.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    score = file_freshness(row, current_commit_sha="abc123", now=now)
    assert score == 1.0


def test_bundle_freshness_takes_max_across_files() -> None:
    """Bundle freshness = max(file_freshness for f in bundle.files)."""
    from pathlib import Path
    from autofix.crawl.score import bundle_freshness

    bundle = MagicMock()
    bundle.file_paths = (Path("file_a.py"), Path("file_b.py"))

    ledger = MagicMock()
    # File a hasn't drifted (freshness ~0.2). File b HAS drifted (freshness 1.0).
    def _latest(path, analyzer):
        r = MagicMock()
        if "b" in str(path):
            r.last_commit_sha = "different"
            r.last_scanned_at = "2026-05-07T00:00:00Z"
        else:
            r.last_commit_sha = "current"
            r.last_scanned_at = "2026-05-08T00:00:00Z"
        return r
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
