"""Relevance scoring (ARCH-016 AC-7, AC-9)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock



def test_relevance_in_zero_one() -> None:
    """Whatever the inputs, relevance must be in [0, 1]."""
    from autofix.crawl.score import relevance

    git_log = MagicMock()
    git_log.days_since_last_commit.return_value = 0
    git_log.commits_in_last_30_days.return_value = 0
    git_log.incoming_dependency_count.return_value = 0

    score = relevance(Path("a.py"), root=Path("/tmp"), git_log=git_log)
    assert 0.0 <= score <= 1.0


def test_recent_high_churn_scores_high() -> None:
    """File committed today, churned a lot, imported by many → high relevance."""
    from autofix.crawl.score import relevance

    git_log = MagicMock()
    git_log.days_since_last_commit.return_value = 0
    git_log.commits_in_last_30_days.return_value = 20
    git_log.incoming_dependency_count.return_value = 15

    score = relevance(Path("a.py"), root=Path("/tmp"), git_log=git_log)
    assert score > 0.9


def test_old_no_churn_scores_low() -> None:
    """Untouched in 90 days, never churned, no imports → low relevance."""
    from autofix.crawl.score import relevance

    git_log = MagicMock()
    git_log.days_since_last_commit.return_value = 90
    git_log.commits_in_last_30_days.return_value = 0
    git_log.incoming_dependency_count.return_value = 0

    score = relevance(Path("a.py"), root=Path("/tmp"), git_log=git_log)
    assert score < 0.05


def test_recency_exponential_decay() -> None:
    """Recency = exp(-days/7). Days=0 → ~1.0, Days=7 → ~0.37."""
    from autofix.crawl.score import relevance

    git_log_today = MagicMock()
    git_log_today.days_since_last_commit.return_value = 0
    git_log_today.commits_in_last_30_days.return_value = 0
    git_log_today.incoming_dependency_count.return_value = 0

    git_log_week = MagicMock()
    git_log_week.days_since_last_commit.return_value = 7
    git_log_week.commits_in_last_30_days.return_value = 0
    git_log_week.incoming_dependency_count.return_value = 0

    today = relevance(Path("a.py"), root=Path("/tmp"), git_log=git_log_today)
    week = relevance(Path("a.py"), root=Path("/tmp"), git_log=git_log_week)

    # Recency-only contributions: 0.5 * 1.0 = 0.5 vs 0.5 * exp(-1) ~ 0.184
    assert abs(today - 0.5) < 0.01
    assert abs(week - 0.184) < 0.02


def test_churn_caps_at_ten_commits() -> None:
    """Churn = min(1, commits/10). 10+ commits in 30 days = max contribution."""
    from autofix.crawl.score import relevance

    g1 = MagicMock()
    g1.days_since_last_commit.return_value = 999
    g1.commits_in_last_30_days.return_value = 10
    g1.incoming_dependency_count.return_value = 0
    g2 = MagicMock()
    g2.days_since_last_commit.return_value = 999
    g2.commits_in_last_30_days.return_value = 100
    g2.incoming_dependency_count.return_value = 0

    s10 = relevance(Path("a.py"), root=Path("/tmp"), git_log=g1)
    s100 = relevance(Path("a.py"), root=Path("/tmp"), git_log=g2)

    # Both should land at the churn cap × weight (~ 0.3).
    assert abs(s10 - s100) < 1e-9


def test_centrality_caps_at_ten_imports() -> None:
    from autofix.crawl.score import relevance

    g1 = MagicMock()
    g1.days_since_last_commit.return_value = 999
    g1.commits_in_last_30_days.return_value = 0
    g1.incoming_dependency_count.return_value = 10
    g2 = MagicMock()
    g2.days_since_last_commit.return_value = 999
    g2.commits_in_last_30_days.return_value = 0
    g2.incoming_dependency_count.return_value = 100

    s10 = relevance(Path("a.py"), root=Path("/tmp"), git_log=g1)
    s100 = relevance(Path("a.py"), root=Path("/tmp"), git_log=g2)

    assert abs(s10 - s100) < 1e-9


def test_non_git_fallback() -> None:
    """When git_log is empty, recency/churn fall back to 0.5
    centrality
    also falls back if SCIP isn't available."""
    from autofix.crawl.score import relevance

    git_log = MagicMock()
    git_log.is_empty.return_value = True
    git_log.days_since_last_commit.return_value = None
    git_log.commits_in_last_30_days.return_value = None
    git_log.incoming_dependency_count.return_value = None

    score = relevance(Path("a.py"), root=Path("/tmp"), git_log=git_log)
    # Three fallback halves × weights = 0.5 * 0.5 + 0.3 * 0.5 + 0.2 * 0.5 = 0.5
    assert abs(score - 0.5) < 0.01
