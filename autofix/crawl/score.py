"""Freshness, relevance, priority — pure scoring functions (ARCH-016).

Three composable scores in ``[0.0, 1.0]``:

* :func:`file_freshness` — per-file. ``1.0`` when the file's
  ``last_commit_sha`` has drifted from ``current_commit_sha``;
  otherwise time-decay over a fixed
  :data:`STALENESS_HORIZON_HOURS` horizon.
* :func:`bundle_freshness` — max of file_freshness across the
  bundle's files. Unseen files (no ledger row for the
  ``(fingerprint, analyzer)`` key on any of the bundle's files)
  count as ``1.0`` (maximally stale).
* :func:`relevance` — per-path. Weighted sum of recency
  (exponential decay over 7 days), churn (capped at 10
  commits/30d), centrality (capped at 10 import fanout). Weights
  come from :mod:`crawl_constants`.
* :func:`priority` — ``bundle_freshness × relevance(seed)``.

All scoring is pure — no I/O, no global state. Inputs are either
ledger rows (for freshness) or a duck-typed ``git_log`` adapter
(for relevance) plus a path. Tests mock both.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autofix.crawl.crawl_constants import (
    CENTRALITY_CAP_FANOUT,
    CHURN_CAP_COMMITS,
    NON_GIT_FALLBACK_SCORE,
    RECENCY_DECAY_DAYS,
    RELEVANCE_WEIGHT_CENTRALITY,
    RELEVANCE_WEIGHT_CHURN,
    RELEVANCE_WEIGHT_RECENCY,
    STALENESS_HORIZON_HOURS,
)


def _parse_iso_z(s: str) -> datetime:
    """Parse an ``YYYY-MM-DDTHH:MM:SSZ`` string into an aware UTC datetime."""
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def file_freshness(
    row: Any,
    current_commit_sha: str,
    *,
    staleness_horizon_hours: int = STALENESS_HORIZON_HOURS,
    now: datetime | None = None,
) -> float:
    """Per-file freshness score.

    Returns ``1.0`` when ``row.last_commit_sha`` differs from
    ``current_commit_sha`` (the file changed → re-scan ASAP).
    Otherwise returns the wall-clock time-decay
    ``min(1.0, age_hours / staleness_horizon_hours)``.
    """
    if row.last_commit_sha != current_commit_sha:
        return 1.0

    if now is None:
        now = datetime.now(timezone.utc)
    last = _parse_iso_z(row.last_scanned_at)
    age_hours = (now - last).total_seconds() / 3600
    if age_hours <= 0:
        return 0.0
    return min(1.0, age_hours / staleness_horizon_hours)


def bundle_freshness(bundle: Any, ledger: Any, current_commit_sha: str) -> float:
    """Bundle freshness = max(file_freshness for f in bundle.files).

    Files unseen by the ledger contribute ``1.0`` (maximally stale).
    The "max" reflects: if ANY file in the bundle has changed, the
    whole bundle's prior verdict is potentially stale.
    """
    best = 0.0
    for path in bundle.file_paths:
        # The ledger's latest_for is keyed on the bundle's fingerprint
        # in the production caller, but for per-file freshness we look
        # up by file path. Adapters may return None for unseen files.
        row = _latest_per_file(ledger, path)
        if row is None:
            return 1.0
        score = file_freshness(row, current_commit_sha)
        if score > best:
            best = score
    return best


def _latest_per_file(ledger: Any, path: Path) -> Any | None:
    """Best-effort lookup: ledger may expose latest_for(fp, analyzer)
    keyed by fingerprint (the production case) or latest_for(path,
    analyzer) keyed by path (test mocks). We try both shapes.
    """
    # The test mocks call ``ledger.latest_for(path, analyzer)`` with a
    # single ``analyzer`` we don't have here — pass ``None`` and let
    # the mock return its default.
    return ledger.latest_for(path, None)


def relevance(
    path: Path,
    *,
    root: Path,
    git_log: Any,
    now: datetime | None = None,
) -> float:
    """Per-path relevance.

    ``relevance = w_recency * recency + w_churn * churn +
    w_centrality * centrality``, where:

    * ``recency = exp(-days_since_last_commit / 7)`` — files
      committed today get ~1.0; files untouched 30 days get ~0.01.
    * ``churn = min(1.0, commits_in_last_30_days / 10)`` —
      capped at 10 commits / month.
    * ``centrality = min(1.0, import_fanout / 10)`` — capped at
      10 inbound imports.

    When ``git_log.is_empty()`` is True (non-git tree), recency
    and churn fall back to ``0.5`` and centrality also falls back
    to ``0.5`` if the git_log can't compute it.
    """
    # Each subscore independently falls back when its underlying
    # git_log method returns ``None`` (non-git tree, no commit
    # history, no SCIP index for centrality, etc.). The overall
    # ``is_empty`` heuristic is unnecessary — None returns from the
    # individual probes are the canonical "no data" signal.
    days = git_log.days_since_last_commit(path)
    if days is None:
        recency = NON_GIT_FALLBACK_SCORE
    else:
        recency = math.exp(-days / RECENCY_DECAY_DAYS)
    recency = max(0.0, min(1.0, recency))

    cnt = git_log.commits_in_last_30_days(path)
    if cnt is None:
        churn = NON_GIT_FALLBACK_SCORE
    else:
        churn = min(1.0, cnt / CHURN_CAP_COMMITS)

    fanout = git_log.import_fanout(path)
    if fanout is None:
        centrality = NON_GIT_FALLBACK_SCORE
    else:
        centrality = min(1.0, fanout / CENTRALITY_CAP_FANOUT)

    score = (
        RELEVANCE_WEIGHT_RECENCY * recency
        + RELEVANCE_WEIGHT_CHURN * churn
        + RELEVANCE_WEIGHT_CENTRALITY * centrality
    )
    return max(0.0, min(1.0, score))


def priority(
    bundle: Any,
    ledger: Any,
    current_commit_sha: str,
    *,
    root: Path,
    git_log: Any,
) -> float:
    """``priority = bundle_freshness × relevance(seed_path)``."""
    fresh = bundle_freshness(bundle, ledger, current_commit_sha)
    rel = relevance(bundle.seed_path, root=root, git_log=git_log)
    return fresh * rel


__all__ = [
    "file_freshness",
    "bundle_freshness",
    "relevance",
    "priority",
]
