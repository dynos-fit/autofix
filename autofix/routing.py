#!/usr/bin/env python3
"""Routing helpers for autofix policy and Q-learning decisions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from autofix.defaults import (
    AUTOFIX_REWARD_GIT_COMMIT_FAILED,
    AUTOFIX_REWARD_ISSUE_OPENED,
    AUTOFIX_REWARD_NO_CHANGES,
    AUTOFIX_REWARD_PR_MERGED,
    AUTOFIX_REWARD_PR_OPENED,
    AUTOFIX_REWARD_SKIP,
    AUTOFIX_REWARD_VERIFICATION_FAILED,
    FINDING_MAX_AGE_DAYS,
    MAX_ATTEMPTS,
)
from autofix.platform import build_import_graph


def check_category_health(category: str, findings: list[dict]) -> tuple[str, str]:
    """Check whether a category should be temporarily disabled."""

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    failure_count = 0
    for finding in findings:
        if finding.get("category") != category:
            continue
        status = finding.get("status", "")
        if status not in ("failed", "permanently_failed"):
            continue
        found_at = finding.get("found_at", "")
        if not found_at:
            continue
        try:
            found_dt = datetime.fromisoformat(found_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if found_dt >= cutoff:
            failure_count += 1
    if failure_count >= MAX_ATTEMPTS:
        return "disabled", f"{failure_count} failures in last {FINDING_MAX_AGE_DAYS} days"
    return "ok", ""


def compute_centrality_tier(file_path: str, root: Path) -> str:
    """Derive centrality tier from PageRank quartiles: high/medium/low."""

    try:
        graph = build_import_graph(root)
        pagerank = graph.get("pagerank", {})
        if not pagerank:
            return "medium"
        values = sorted(pagerank.values())
        if not values:
            return "medium"
        n = len(values)
        q25 = values[n // 4] if n >= 4 else values[0]
        q75 = values[(3 * n) // 4] if n >= 4 else values[-1]
        file_score = pagerank.get(file_path, 0.0)
        if file_score >= q75:
            return "high"
        if file_score <= q25:
            return "low"
        return "medium"
    except Exception:
        return "medium"


def compute_autofix_reward(finding: dict) -> float:
    """Compute Q-learning reward from a processed finding outcome."""

    status = finding.get("status", "")
    fail_reason = str(finding.get("fail_reason", "") or "")
    merge_outcome = finding.get("merge_outcome", "")

    if status == "fixed":
        if merge_outcome == "merged":
            return AUTOFIX_REWARD_PR_MERGED
        return AUTOFIX_REWARD_PR_OPENED
    if status == "issue-opened":
        return AUTOFIX_REWARD_ISSUE_OPENED
    if status == "suppressed-policy" and finding.get("suppression_reason") == "q-learning:skip":
        return AUTOFIX_REWARD_SKIP
    if status == "failed":
        if "claude_no_changes" in fail_reason:
            return AUTOFIX_REWARD_NO_CHANGES
        if "verification_failed" in fail_reason:
            return AUTOFIX_REWARD_VERIFICATION_FAILED
        if "git_commit_failed" in fail_reason:
            return AUTOFIX_REWARD_GIT_COMMIT_FAILED
        return AUTOFIX_REWARD_NO_CHANGES
    return AUTOFIX_REWARD_SKIP
