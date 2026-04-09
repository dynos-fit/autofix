#!/usr/bin/env python3
"""State, policy, and persistence helpers for autofix."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autofix.defaults import (
    COOLDOWN_AFTER_FAILURES,
    CONF_AUTOFIX_BASE,
    CONF_DEAD_CODE,
    CONF_ISSUE_ONLY,
    CONF_LLM_REVIEW,
    CONF_SYNTAX_ERROR,
    FINDING_MAX_AGE_DAYS,
    MAX_FINDINGS_ENTRIES,
    MAX_OPEN_PRS,
    MAX_PRS_PER_DAY,
    MIN_CONF_AUTOFIX,
    RECENT_PRS_COUNT,
)
from autofix.platform import (
    aggregate_state_dir,
    current_state_dir,
    load_json,
    now_iso,
    persistent_project_dir,
    runtime_state_dir,
    write_scan_artifact,
    write_state_snapshot,
    write_json,
)

VALID_CATEGORIES = {
    "recurring-audit",
    "dependency-vuln",
    "dead-code",
    "architectural-drift",
    "syntax-error",
    "llm-review",
}


def findings_path(root: Path) -> Path:
    return current_state_dir(root) / "findings.json"


def autofix_policy_path(root: Path) -> Path:
    return persistent_project_dir(root) / "autofix-policy.json"


def autofix_metrics_path(root: Path) -> Path:
    return current_state_dir(root) / "metrics.json"


def autofix_benchmarks_path(root: Path) -> Path:
    return current_state_dir(root) / "benchmarks.json"


def scan_coverage_path(root: Path) -> Path:
    return current_state_dir(root) / "scan-coverage.json"


def _migrate_legacy_state_layout(root: Path) -> None:
    aggregate_dir = aggregate_state_dir(root)
    current_dir = current_state_dir(root)
    legacy_files = {
        "findings.json": [
            aggregate_dir / "findings.json",
            runtime_state_dir(root) / "latest-findings.json",
            runtime_state_dir(root) / "proactive-findings.json",
        ],
        "scan-coverage.json": [
            aggregate_dir / "scan-coverage.json",
            runtime_state_dir(root) / "latest-scan-coverage.json",
        ],
        "metrics.json": [
            aggregate_dir / "metrics.json",
            persistent_project_dir(root) / "latest-metrics.json",
            persistent_project_dir(root) / "autofix-metrics.json",
        ],
        "benchmarks.json": [
            aggregate_dir / "benchmarks.json",
            persistent_project_dir(root) / "latest-benchmarks.json",
            persistent_project_dir(root) / "autofix-benchmarks.json",
        ],
    }
    current_dir.mkdir(parents=True, exist_ok=True)
    for current_name, candidates in legacy_files.items():
        current_path = current_dir / current_name
        if current_path.exists():
            continue
        for legacy_path in candidates:
            if legacy_path == current_path or not legacy_path.exists():
                continue
            current_path.write_text(legacy_path.read_text())
            try:
                legacy_path.unlink()
            except OSError:
                pass
            break


def default_category_policy(category: str) -> dict:
    mode = "autofix"
    base_confidence = CONF_AUTOFIX_BASE
    if category == "syntax-error":
        base_confidence = CONF_SYNTAX_ERROR
    elif category == "dead-code":
        base_confidence = CONF_DEAD_CODE
    elif category == "llm-review":
        base_confidence = CONF_LLM_REVIEW
    elif category in {"dependency-vuln", "architectural-drift", "recurring-audit"}:
        mode = "issue-only"
        base_confidence = CONF_ISSUE_ONLY
    return {
        "enabled": True,
        "mode": mode,
        "min_confidence_autofix": MIN_CONF_AUTOFIX,
        "confidence": base_confidence,
        "stats": {
            "proposed": 0,
            "merged": 0,
            "closed_unmerged": 0,
            "reverted": 0,
            "verification_failed": 0,
            "issues_opened": 0,
        },
    }


def default_autofix_policy() -> dict:
    return {
        "max_prs_per_day": MAX_PRS_PER_DAY,
        "max_open_prs": MAX_OPEN_PRS,
        "cooldown_after_failures": COOLDOWN_AFTER_FAILURES,
        "allow_dependency_file_changes": False,
        "suppressions": [],
        "categories": {
            category: default_category_policy(category)
            for category in sorted(VALID_CATEGORIES)
        },
    }


def normalize_autofix_policy(data: dict | None) -> dict:
    default = default_autofix_policy()
    if not isinstance(data, dict):
        return default
    merged = dict(default)
    if isinstance(data.get("max_prs_per_day"), int) and data["max_prs_per_day"] > 0:
        merged["max_prs_per_day"] = data["max_prs_per_day"]
    if isinstance(data.get("max_open_prs"), int) and data["max_open_prs"] >= 0:
        merged["max_open_prs"] = data["max_open_prs"]
    if isinstance(data.get("cooldown_after_failures"), int) and data["cooldown_after_failures"] >= 0:
        merged["cooldown_after_failures"] = data["cooldown_after_failures"]
    if isinstance(data.get("allow_dependency_file_changes"), bool):
        merged["allow_dependency_file_changes"] = data["allow_dependency_file_changes"]
    if isinstance(data.get("suppressions"), list):
        merged["suppressions"] = data["suppressions"]

    categories = dict(default["categories"])
    data_categories = data.get("categories", {})
    if isinstance(data_categories, dict):
        for category in VALID_CATEGORIES:
            base = dict(categories[category])
            incoming = data_categories.get(category, {})
            if isinstance(incoming, dict):
                if isinstance(incoming.get("enabled"), bool):
                    base["enabled"] = incoming["enabled"]
                if incoming.get("mode") in {"autofix", "issue-only", "disabled"}:
                    base["mode"] = incoming["mode"]
                if isinstance(incoming.get("min_confidence_autofix"), (int, float)):
                    base["min_confidence_autofix"] = float(incoming["min_confidence_autofix"])
                if isinstance(incoming.get("confidence"), (int, float)):
                    base["confidence"] = round(float(incoming["confidence"]), 3)
                stats = dict(base["stats"])
                incoming_stats = incoming.get("stats", {})
                if isinstance(incoming_stats, dict):
                    for key in stats:
                        if isinstance(incoming_stats.get(key), int) and incoming_stats[key] >= 0:
                            stats[key] = incoming_stats[key]
                base["stats"] = stats
            categories[category] = base
    merged["categories"] = categories
    return merged


def load_autofix_policy(root: Path) -> dict:
    path = autofix_policy_path(root)
    if not path.exists() or not path.read_text().strip():
        data = default_autofix_policy()
        write_json(path, data)
        return data
    try:
        raw = load_json(path)
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        raw = {}
    data = normalize_autofix_policy(raw)
    if data != raw:
        write_json(path, data)
    return data


def save_autofix_policy(root: Path, policy: dict) -> None:
    write_json(autofix_policy_path(root), normalize_autofix_policy(policy))


def load_findings(root: Path, *, log: callable | None = None) -> list[dict]:
    _migrate_legacy_state_layout(root)
    path = findings_path(root)
    if not path.exists():
        return []
    try:
        data = load_json(path)
    except (json.JSONDecodeError, OSError) as exc:
        if log:
            log(f"Warning: could not load findings file: {exc}")
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        return data["findings"]
    return []


def save_findings(root: Path, findings: list[dict]) -> None:
    _migrate_legacy_state_layout(root)
    write_state_snapshot(root, "findings.json", {"findings": findings})
    write_scan_artifact(root, "aggregate-findings.json", findings)


def prune_findings(
    findings: list[dict],
    max_age_days: int = FINDING_MAX_AGE_DAYS,
    max_entries: int = MAX_FINDINGS_ENTRIES,
) -> list[dict]:
    now = datetime.now(timezone.utc)
    preserved_statuses = {"fixed", "issue-opened"}
    pruned: list[dict] = []
    for finding in findings:
        status = finding.get("status", "")
        if status in preserved_statuses:
            pruned.append(finding)
            continue
        found_at = finding.get("found_at", "")
        if found_at:
            try:
                found_dt = datetime.fromisoformat(found_at.replace("Z", "+00:00"))
                age_days = (now - found_dt).total_seconds() / 86400
                if age_days > max_age_days:
                    continue
            except (ValueError, TypeError):
                pass
        pruned.append(finding)

    if len(pruned) > max_entries:
        preserved = [f for f in pruned if f.get("status", "") in preserved_statuses]
        non_preserved = [f for f in pruned if f.get("status", "") not in preserved_statuses]
        non_preserved.sort(key=lambda f: f.get("found_at", ""), reverse=True)
        budget = max(max_entries - len(preserved), 0)
        pruned = preserved + non_preserved[:budget]
    return pruned


def description_hash(description: str) -> str:
    return hashlib.sha256(description.encode("utf-8")).hexdigest()[:16]


def make_finding(
    finding_id: str,
    severity: str,
    category: str,
    description: str,
    evidence: dict,
) -> dict:
    return {
        "finding_id": finding_id,
        "severity": severity,
        "category": category,
        "description": description,
        "evidence": evidence,
        "status": "new",
        "found_at": now_iso(),
        "processed_at": None,
        "attempt_count": 0,
        "pr_number": None,
        "pr_url": None,
        "pr_state": None,
        "merge_outcome": None,
        "branch_name": None,
        "issue_number": None,
        "issue_url": None,
        "suppressed_until": None,
        "suppression_reason": None,
        "fail_reason": None,
        "fixability": None,
        "confidence_score": None,
        "rollout_mode": None,
        "verification": {},
        "pr_quality_score": None,
    }


def load_scan_coverage(root: Path) -> dict:
    _migrate_legacy_state_layout(root)
    path = scan_coverage_path(root)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"files": {}}


def save_scan_coverage(root: Path, coverage: dict) -> None:
    _migrate_legacy_state_layout(root)
    write_state_snapshot(root, "scan-coverage.json", coverage)
    write_scan_artifact(root, "scan-coverage.json", coverage)


def dedup_finding(finding: dict, existing: list[dict]) -> str | None:
    fid = finding["finding_id"]
    desc_h = description_hash(finding["description"])
    cat = finding["category"]
    evidence_file = finding.get("evidence", {}).get("file", "")

    for existing_finding in existing:
        ex_id = existing_finding.get("finding_id", "")
        ex_status = existing_finding.get("status", "")
        if ex_id != fid:
            continue
        if ex_status == "permanently_failed":
            return "permanently_failed"
        if ex_status == "fixed" and existing_finding.get("pr_number"):
            return "fixed with merged PR, permanently suppressed"
        return f"exact finding_id match (status={ex_status})"

    for existing_finding in existing:
        ex_status = existing_finding.get("status", "")
        ex_cat = existing_finding.get("category", "")
        ex_file = existing_finding.get("evidence", {}).get("file", "")
        ex_desc_h = description_hash(existing_finding.get("description", ""))
        if ex_cat == cat and ex_file == evidence_file and ex_desc_h == desc_h:
            if ex_status in ("fixed", "issue-opened", "failed", "permanently_failed"):
                return f"semantic match (category={cat}, desc_hash={desc_h}, status={ex_status})"
    return None


def suppression_reason(finding: dict, policy: dict) -> str | None:
    for entry in policy.get("suppressions", []):
        if not isinstance(entry, dict):
            continue
        until = str(entry.get("until", "") or "")
        if until:
            try:
                until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
                if until_dt < datetime.now(timezone.utc):
                    continue
            except ValueError:
                pass
        finding_id = str(entry.get("finding_id", "") or "")
        if finding_id and finding_id == finding.get("finding_id"):
            return str(entry.get("reason", "suppressed by finding id"))
        category = str(entry.get("category", "") or "")
        if category and category != finding.get("category"):
            continue
        path_prefix = str(entry.get("path_prefix", "") or "")
        evidence_file = str(finding.get("evidence", {}).get("file", "") or "")
        if path_prefix and not evidence_file.startswith(path_prefix):
            continue
        if category or path_prefix:
            return str(entry.get("reason", "suppressed by policy"))
    return None


def rate_limit_snapshot(policy: dict, findings: list[dict], config: dict | None = None) -> dict:
    now = datetime.now(timezone.utc)
    today = now.date()
    open_prs = 0
    prs_today = 0
    recent_failures = 0
    for finding in findings:
        if finding.get("pr_number") and finding.get("merge_outcome") in (None, "open"):
            open_prs += 1
        processed_at = str(finding.get("processed_at", "") or "")
        if processed_at:
            try:
                processed_dt = datetime.fromisoformat(processed_at.replace("Z", "+00:00"))
                if processed_dt.date() == today and finding.get("pr_number"):
                    prs_today += 1
                if processed_dt > now - timedelta(days=1) and finding.get("status") in {"failed", "permanently_failed"}:
                    recent_failures += 1
            except ValueError:
                pass
    # config.json overrides take priority over policy for rate limits
    cfg = config or {}
    return {
        "prs_today": prs_today,
        "open_prs": open_prs,
        "recent_failures": recent_failures,
        "max_prs_per_day": int(cfg.get("max_prs_per_day", policy.get("max_prs_per_day", MAX_PRS_PER_DAY)) or MAX_PRS_PER_DAY),
        "max_open_prs": int(cfg.get("max_open_prs", policy.get("max_open_prs", MAX_OPEN_PRS)) or MAX_OPEN_PRS),
        "cooldown_after_failures": int(policy.get("cooldown_after_failures", COOLDOWN_AFTER_FAILURES) or COOLDOWN_AFTER_FAILURES),
    }


def rate_limit_reason(policy: dict, findings: list[dict], config: dict | None = None) -> str | None:
    snapshot = rate_limit_snapshot(policy, findings, config=config)
    if snapshot["prs_today"] >= snapshot["max_prs_per_day"]:
        return f"max_prs_per_day reached ({snapshot['prs_today']}/{snapshot['max_prs_per_day']})"
    if snapshot["open_prs"] >= snapshot["max_open_prs"]:
        return f"max_open_prs reached ({snapshot['open_prs']}/{snapshot['max_open_prs']})"
    if snapshot["recent_failures"] >= snapshot["cooldown_after_failures"]:
        return f"cooldown_after_failures reached ({snapshot['recent_failures']}/{snapshot['cooldown_after_failures']})"
    return None


def recompute_category_confidence(policy: dict) -> dict:
    categories = policy.get("categories", {})
    for category, config in categories.items():
        if not isinstance(config, dict):
            continue
        stats = config.get("stats", {})
        if not isinstance(stats, dict):
            continue
        merged = int(stats.get("merged", 0) or 0)
        closed_unmerged = int(stats.get("closed_unmerged", 0) or 0)
        reverted = int(stats.get("reverted", 0) or 0)
        verification_failed = int(stats.get("verification_failed", 0) or 0)
        prior_success = 2.0
        prior_failure = 1.0
        failures = closed_unmerged + reverted + verification_failed
        confidence = (merged + prior_success) / (merged + failures + prior_success + prior_failure)
        if category == "syntax-error":
            confidence = max(confidence, 0.9)
        config["confidence"] = round(confidence, 3)
    return policy


def build_autofix_benchmarks(root: Path, findings: list[dict], policy: dict) -> dict:
    categories: dict[str, dict] = {}
    recent_prs: list[dict] = []
    for finding in findings:
        category = str(finding.get("category", "unknown"))
        bucket = categories.setdefault(category, {
            "findings": 0,
            "autofix_prs": 0,
            "merged": 0,
            "closed_unmerged": 0,
            "reverted": 0,
            "issues_opened": 0,
            "verification_failed": 0,
            "avg_pr_quality_score": 0.0,
            "_quality_scores": [],
        })
        bucket["findings"] += 1
        status = finding.get("status")
        outcome = finding.get("merge_outcome")
        if status == "issue-opened":
            bucket["issues_opened"] += 1
        if str(finding.get("fail_reason", "")).startswith("verification_failed"):
            bucket["verification_failed"] += 1
        if finding.get("pr_number"):
            bucket["autofix_prs"] += 1
            if outcome == "merged":
                bucket["merged"] += 1
            elif outcome == "closed_unmerged":
                bucket["closed_unmerged"] += 1
            elif outcome == "reverted":
                bucket["reverted"] += 1
            pr_quality = finding.get("pr_quality_score")
            if isinstance(pr_quality, (int, float)):
                bucket["_quality_scores"].append(float(pr_quality))
            recent_prs.append({
                "finding_id": finding.get("finding_id"),
                "category": category,
                "number": finding.get("pr_number"),
                "state": (finding.get("pr_state") or "UNKNOWN").upper(),
                "merge_outcome": outcome,
                "title": finding.get("description", ""),
                "created_at": finding.get("processed_at"),
                "url": finding.get("pr_url"),
                "branch": finding.get("branch_name"),
            })
    for bucket in categories.values():
        scores = bucket.pop("_quality_scores")
        bucket["avg_pr_quality_score"] = round(sum(scores) / len(scores), 3) if scores else 0.0
        pr_count = bucket["autofix_prs"]
        bucket["merge_rate"] = round(bucket["merged"] / pr_count, 3) if pr_count else 0.0
    recent_prs.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    benchmarks = {
        "generated_at": now_iso(),
        "categories": categories,
        "recent_prs": recent_prs[:RECENT_PRS_COUNT],
        "policy": {
            "max_prs_per_day": policy.get("max_prs_per_day"),
            "max_open_prs": policy.get("max_open_prs"),
        },
    }
    _migrate_legacy_state_layout(root)
    write_state_snapshot(root, "benchmarks.json", benchmarks)
    write_scan_artifact(root, "autofix-benchmarks.json", benchmarks)
    return benchmarks


def write_autofix_metrics(root: Path, findings: list[dict], policy: dict) -> dict:
    snapshot = rate_limit_snapshot(policy, findings)
    suppressions = policy.get("suppressions", [])
    categories = {}
    for category, config in policy.get("categories", {}).items():
        if not isinstance(config, dict):
            continue
        stats = config.get("stats", {})
        categories[category] = {
            "mode": config.get("mode", "issue-only"),
            "enabled": bool(config.get("enabled", True)),
            "confidence": config.get("confidence", 0.0),
            "merged": int(stats.get("merged", 0) or 0),
            "closed_unmerged": int(stats.get("closed_unmerged", 0) or 0),
            "reverted": int(stats.get("reverted", 0) or 0),
            "issues_opened": int(stats.get("issues_opened", 0) or 0),
            "verification_failed": int(stats.get("verification_failed", 0) or 0),
        }
    totals = {
        "findings": len(findings),
        "open_prs": snapshot["open_prs"],
        "prs_today": snapshot["prs_today"],
        "recent_failures": snapshot["recent_failures"],
        "suppression_count": len(suppressions),
        "merged": sum(v["merged"] for v in categories.values()),
        "closed_unmerged": sum(v["closed_unmerged"] for v in categories.values()),
        "reverted": sum(v["reverted"] for v in categories.values()),
        "issues_opened": sum(v["issues_opened"] for v in categories.values()),
    }
    metrics = {
        "generated_at": now_iso(),
        "totals": totals,
        "rate_limits": snapshot,
        "categories": categories,
        "recent_prs": build_autofix_benchmarks(root, findings, policy).get("recent_prs", []),
    }
    _migrate_legacy_state_layout(root)
    write_state_snapshot(root, "metrics.json", metrics)
    write_scan_artifact(root, "autofix-metrics.json", metrics)
    return metrics
