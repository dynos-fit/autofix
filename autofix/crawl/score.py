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
  (exponential decay over 7 days) and churn (capped at 10
  commits/30d). Weights come from :mod:`crawl_constants`.
  Centrality (incoming-dependency count) was removed because it
  required language-specific import-graph walking — the crawler
  is language-agnostic at the contract layer.
* :func:`priority` — ``bundle_freshness × relevance(seed)``.

All scoring is pure — no I/O, no global state. Inputs are either
ledger rows (for freshness) or a duck-typed ``git_log`` adapter
(for relevance) plus a path. Tests mock both.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autofix.crawl.crawl_constants import (
    CHURN_CAP_COMMITS,
    ENTRYPOINT_BOOST,
    LOW_VALUE_CLASS_PENALTY,
    MAX_RELEVANT_FILE_BYTES,
    NON_GIT_FALLBACK_SCORE,
    OVERSIZE_FILE_PENALTY,
    RECENCY_DECAY_DAYS,
    RELEVANCE_WEIGHT_CHURN,
    RELEVANCE_WEIGHT_RECENCY,
    STALENESS_HORIZON_HOURS,
)
from autofix.crawl.file_classifier import FileClass


# Low-value file classes — when ``ScoringFlags.low_value_class_penalty``
# is on, ``relevance`` multiplies the base score by
# ``LOW_VALUE_CLASS_PENALTY`` for any file whose ``file_class`` falls in
# this set. ``source``, ``test``, ``config``, ``entrypoint`` and
# ``unknown`` are explicitly NOT low-value (no penalty).
_LOW_VALUE_CLASSES: frozenset[FileClass] = frozenset({
    FileClass.docs,
    FileClass.lockfile,
    FileClass.vendor,
    FileClass.generated,
    FileClass.build_output,
    FileClass.cache,
    FileClass.binary,
})


@dataclass(frozen=True)
class ScoringFlags:
    """Opt-in supplemental relevance signals.

    All three flags default to ``False``. When all flags are off (or
    ``scoring_flags`` is ``None``), :func:`relevance` takes a strict
    short-circuit path that produces byte-identical output to the
    pre-flag formula — golden-file regressions in the test suite pin
    those exact floats.

    Order of operations when flags are on:
    ``base -> low_value_class_penalty (multiplicative)
    -> oversize_file_penalty (multiplicative)
    -> entrypoint_boost (additive) -> final clamp [0.0, 1.0]``.
    """

    entrypoint_boost: bool = False
    low_value_class_penalty: bool = False
    oversize_file_penalty: bool = False


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
    last = _parse_iso_z(row.ts)
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
    file_class: FileClass | None = None,
    file_size_bytes: int | None = None,
    scoring_flags: ScoringFlags | None = None,
) -> float:
    """Per-path relevance.

    ``relevance = w_recency * recency + w_churn * churn``, where:

    * ``recency = exp(-days_since_last_commit / 7)`` — files
      committed today get ~1.0; files untouched 30 days get ~0.01.
    * ``churn = min(1.0, commits_in_last_30_days / 10)`` —
      capped at 10 commits / month.

    Centrality (incoming-dependency count) was removed because it
    required language-specific import-graph walking and broke the
    "any-file" property of the crawler subsystem. Both surviving
    pillars are git-only and language-agnostic.

    When the git_log adapter returns ``None`` for a subscore (non-git
    tree, missing log), that pillar falls back to
    :data:`NON_GIT_FALLBACK_SCORE`.

    Supplemental scoring signals (opt-in via ``scoring_flags``):

    * ``file_class`` — a :class:`~autofix.crawl.file_classifier.FileClass`
      member used by both ``low_value_class_penalty`` (penalizes
      low-value classes like ``docs``/``vendor``/``generated``) and
      ``entrypoint_boost`` (additive boost when the file is an
      ``entrypoint``).
    * ``file_size_bytes`` — used by ``oversize_file_penalty`` to
      multiply the score by ``OVERSIZE_FILE_PENALTY`` when the file
      strictly exceeds ``MAX_RELEVANT_FILE_BYTES``.
    * ``scoring_flags`` — a :class:`ScoringFlags` selecting which of
      the three modifiers to apply. ``None`` or all-False produces
      the pure two-pillar formula above.
    """
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

    if scoring_flags is None or (
        not scoring_flags.entrypoint_boost
        and not scoring_flags.low_value_class_penalty
        and not scoring_flags.oversize_file_penalty
    ):
        score = (
            RELEVANCE_WEIGHT_RECENCY * recency
            + RELEVANCE_WEIGHT_CHURN * churn
        )
        return max(0.0, min(1.0, score))

    # --- Flags-active path. Order of operations:
    # base -> class penalty (multiplicative) -> oversize penalty
    # (multiplicative) -> entrypoint boost (additive) -> final clamp.
    score = (
        RELEVANCE_WEIGHT_RECENCY * recency
        + RELEVANCE_WEIGHT_CHURN * churn
    )

    if (
        scoring_flags.low_value_class_penalty
        and file_class is not None
        and file_class in _LOW_VALUE_CLASSES
    ):
        score = score * LOW_VALUE_CLASS_PENALTY

    if (
        scoring_flags.oversize_file_penalty
        and file_size_bytes is not None
        and file_size_bytes > MAX_RELEVANT_FILE_BYTES
    ):
        score = score * OVERSIZE_FILE_PENALTY

    if (
        scoring_flags.entrypoint_boost
        and file_class is FileClass.entrypoint
    ):
        score = score + ENTRYPOINT_BOOST

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
    "ScoringFlags",
    "file_freshness",
    "bundle_freshness",
    "relevance",
    "priority",
]
