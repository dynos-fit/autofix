"""Scanner orchestration for the extracted autofix subsystem."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from autofix.platform import runtime_state_dir


def _update_retrospective_outcome(root: Path, finding: dict) -> None:
    """Update an existing task-retrospective.json when PR outcome is finalized."""
    finding_id = str(finding.get("finding_id", ""))
    if not finding_id:
        return
    task_dir = runtime_state_dir(root) / f"task-autofix-{finding_id}"
    retro_path = task_dir / "task-retrospective.json"
    if not retro_path.is_file():
        return
    try:
        retro = json.loads(retro_path.read_text(encoding="utf-8"))
        merge_outcome = finding.get("merge_outcome", "")
        retro["merge_outcome"] = merge_outcome
        if merge_outcome == "merged":
            retro["task_outcome"] = "MERGED"
        elif merge_outcome == "closed_unmerged":
            retro["task_outcome"] = "CLOSED"
        retro_path.write_text(json.dumps(retro, indent=2), encoding="utf-8")
    except (json.JSONDecodeError, OSError):
        pass


@dataclass(frozen=True)
class ScannerRuntime:
    log: Callable[[str], None]
    now_iso: Callable[[], str]
    log_event: Callable[..., None]
    project_policy: Callable[[Path], dict]
    write_json: Callable[[Path, dict], None]
    findings_path: Callable[[Path], Path]
    load_policy: Callable[[Path], dict]
    save_policy: Callable[[Path, dict], None]
    load_findings: Callable[[Path], list[dict]]
    save_findings: Callable[[Path, list[dict]], None]
    prune_findings: Callable[[list[dict]], list[dict]]
    dedup_finding: Callable[[dict, list[dict]], str | None]
    suppression_reason: Callable[[dict, dict], str | None]
    rate_limit_reason: Callable[[dict, list[dict]], str | None]
    recompute_category_confidence: Callable[[dict], dict]
    write_autofix_metrics: Callable[[Path, list[dict], dict], dict]
    build_autofix_benchmarks: Callable[[Path, list[dict], dict], dict]
    check_category_health: Callable[[str, list[dict]], tuple[str, str]]
    compute_centrality_tier: Callable[[str, Path], str]
    compute_autofix_reward: Callable[[dict], float]
    default_category_policy: Callable[[str], dict]
    classify_fixability: Callable[[dict], str]
    detect_syntax_errors: Callable[[Path], list[dict]]
    detect_recurring_audit: Callable[[Path], list[dict]]
    detect_dependency_vulns: Callable[[Path], list[dict]]
    detect_dead_code: Callable[[Path], list[dict]]
    detect_architectural_drift: Callable[[Path], list[dict]]
    detect_llm_review: Callable[..., list[dict]]
    autofix_finding: Callable[[dict, Path, dict | None], dict]
    open_github_issue: Callable[[dict, Path, dict | None], dict]
    cleanup_merged_branches: Callable[[Path], None]
    encode_autofix_state: Callable[[str, str, str, str], str]
    load_autofix_q_table: Callable[[Path], dict]
    save_autofix_q_table: Callable[[Path, dict], None]
    select_action: Callable[[dict, str, list[str]], tuple[str, str]]
    update_q_value: Callable[[dict, str, str, float, object | None], None]
    save_fix_template: Callable[[Path, dict, str], None]
    gh_api_timeout: int
    max_attempts: int
    min_conf_autofix: float
    scan_timeout_seconds: int
    batch_min_group_size: int
    qlearn_epsilon: float
    pr_feedback_reward_merged: float
    pr_feedback_reward_closed: float


def sync_outcomes(
    root: Path,
    findings: list[dict],
    policy: dict,
    runtime: ScannerRuntime,
) -> tuple[list[dict], dict]:
    if not shutil.which("gh"):
        metrics = runtime.write_autofix_metrics(
            root,
            findings,
            runtime.recompute_category_confidence(policy),
        )
        return findings, metrics

    for finding in findings:
        category = str(finding.get("category", "") or "")
        if category not in policy.get("categories", {}):
            continue
        category_stats = policy["categories"][category]["stats"]
        pr_number = finding.get("pr_number")
        if pr_number:
            try:
                result = subprocess.run(
                    ["gh", "pr", "view", str(pr_number), "--json", "state,mergedAt,closedAt,url"],
                    capture_output=True,
                    text=True,
                    timeout=runtime.gh_api_timeout,
                    cwd=str(root),
                )
                if result.returncode == 0 and result.stdout.strip():
                    data = json.loads(result.stdout)
                    state = str(data.get("state", "OPEN")).upper()
                    finding["pr_state"] = state
                    finding["pr_url"] = data.get("url") or finding.get("pr_url")
                    if state == "MERGED" or data.get("mergedAt"):
                        if finding.get("merge_outcome") != "merged":
                            category_stats["merged"] += 1
                        finding["merge_outcome"] = "merged"
                        finding["merged_at"] = data.get("mergedAt")
                    elif state == "CLOSED":
                        if finding.get("merge_outcome") != "closed_unmerged":
                            category_stats["closed_unmerged"] += 1
                        finding["merge_outcome"] = "closed_unmerged"
                        finding["closed_at"] = data.get("closedAt")
                    else:
                        finding["merge_outcome"] = "open"
            except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
                pass
        issue_number = finding.get("issue_number")
        if issue_number:
            try:
                result = subprocess.run(
                    ["gh", "issue", "view", str(issue_number), "--json", "state,url,closedAt"],
                    capture_output=True,
                    text=True,
                    timeout=runtime.gh_api_timeout,
                    cwd=str(root),
                )
                if result.returncode == 0 and result.stdout.strip():
                    data = json.loads(result.stdout)
                    finding["issue_url"] = data.get("url") or finding.get("issue_url")
                    finding["issue_state"] = str(data.get("state", "OPEN")).upper()
                    finding["issue_closed_at"] = data.get("closedAt")
            except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
                pass

    policy = runtime.recompute_category_confidence(policy)
    runtime.save_policy(root, policy)
    metrics = runtime.write_autofix_metrics(root, findings, policy)
    return findings, metrics


def group_similar_findings(findings: list[dict], batch_min_group_size: int) -> list[list[dict]]:
    """Group findings by file for LLM review, otherwise by exact (category, category_detail)."""
    if not findings:
        return []

    groups: dict[tuple[str, str], list[dict]] = {}
    for finding in findings:
        category = finding.get("category", "")
        evidence_file = str(finding.get("evidence", {}).get("file", "") or "")
        if category == "llm-review" and evidence_file:
            groups.setdefault(("file", evidence_file), []).append(finding)
            continue
        detail = finding.get("category_detail", "") or ""
        groups.setdefault((category, detail), []).append(finding)

    result: list[list[dict]] = []
    for key in sorted(groups.keys()):
        group = groups[key]
        is_file_group = key[0] == "file"
        if len(group) >= batch_min_group_size or (is_file_group and len(group) > 1):
            result.append(group)
            continue
        for finding in group:
            result.append([finding])

    return result


def autofix_batch(
    batch: list[dict],
    root: Path,
    policy: dict,
    runtime: ScannerRuntime,
) -> list[dict]:
    """Fix a batch of similar findings while tracking a shared branch name."""
    if not batch:
        return []

    category = batch[0].get("category", "unknown")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch_name = f"dynos/auto-fix-batch-{category}-{timestamp}"
    evidence_files = {
        str(finding.get("evidence", {}).get("file", "") or "")
        for finding in batch
    }
    same_file_batch = len(evidence_files) == 1 and bool(next(iter(evidence_files), ""))

    results: list[dict] = []
    passing_diffs: list[dict] = []

    if same_file_batch:
        primary = dict(batch[0])
        primary["related_findings"] = [
            {
                "finding_id": finding.get("finding_id"),
                "description": finding.get("description"),
                "severity": finding.get("severity"),
                "category_detail": finding.get("evidence", {}).get("category_detail", ""),
                "file": finding.get("evidence", {}).get("file", ""),
                "line": finding.get("evidence", {}).get("line", 0),
                "confidence_score": finding.get("confidence_score"),
            }
            for finding in batch
        ]
        updated = runtime.autofix_finding(primary, root, policy)
        for original in batch:
            result = dict(original)
            for key in (
                "status",
                "fail_reason",
                "processed_at",
                "pr_number",
                "pr_url",
                "pr_state",
                "merge_outcome",
                "branch_name",
                "verification",
                "pr_quality_score",
                "dry_run",
                "issue_number",
                "issue_url",
                "rollout_mode",
            ):
                if key in updated:
                    result[key] = updated.get(key)
            results.append(result)
        if updated.get("status") == "fixed":
            passing_diffs.append(
                {
                    "finding_id": primary["finding_id"],
                    "verified": True,
                    "diff": updated.get("verification", {}).get("changed_files", []),
                    "finding": updated,
                }
            )
    else:
        for finding in batch:
            finding_id = finding["finding_id"]
            updated = runtime.autofix_finding(finding, root, policy)
            results.append(updated)

            if updated.get("status") == "fixed":
                passing_diffs.append(
                    {
                        "finding_id": finding_id,
                        "verified": True,
                        "diff": updated.get("verification", {}).get("changed_files", []),
                        "finding": updated,
                    }
                )
                continue

            updated["status"] = "failed"
            if "fail_reason" not in updated:
                updated["fail_reason"] = "batch_verification_failed"

    if not [result for result in passing_diffs if result["verified"]]:
        for result in results:
            result["status"] = "failed"
            if "fail_reason" not in result:
                result["fail_reason"] = "no_fixes_passed_verification"
        return results

    for result in results:
        result["batch_branch"] = branch_name

    return results


def check_pr_outcomes(
    root: Path,
    findings: list[dict],
    runtime: ScannerRuntime,
) -> list[dict]:
    """Update Q-learning and saved templates based on merged or closed PRs."""
    try:
        gh_result = subprocess.run(
            ["gh", "pr", "list", "--label", "dynos-autofix", "--state", "all", "--json", "number,state,mergedAt,title"],
            capture_output=True,
            text=True,
            timeout=runtime.gh_api_timeout,
            cwd=str(root),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return findings

    if gh_result.returncode != 0:
        return findings

    try:
        prs = json.loads(gh_result.stdout)
    except (json.JSONDecodeError, ValueError):
        return findings

    if not isinstance(prs, list):
        return findings

    pr_by_number: dict[int, dict] = {}
    for pr in prs:
        number = pr.get("number")
        if isinstance(number, int):
            pr_by_number[number] = pr

    q_table = runtime.load_autofix_q_table(root)
    updated = False

    for finding in findings:
        if finding.get("q_reward_applied"):
            continue

        pr_number = finding.get("pr_number")
        if not pr_number or pr_number not in pr_by_number:
            continue

        pr_info = pr_by_number[pr_number]
        state = str(pr_info.get("state", "")).upper()
        merged_at = pr_info.get("mergedAt")
        evidence_file = str(finding.get("evidence", {}).get("file", ""))
        file_ext = Path(evidence_file).suffix if evidence_file else ""
        category = finding.get("category", "")
        severity = finding.get("severity", "medium")
        centrality_tier = runtime.compute_centrality_tier(evidence_file, root)
        q_state = runtime.encode_autofix_state(category, file_ext, centrality_tier, severity)

        if state == "MERGED" or merged_at:
            runtime.update_q_value(q_table, q_state, "attempt_fix", runtime.pr_feedback_reward_merged, None)
            finding["merge_outcome"] = "merged"
            finding["q_reward_applied"] = True
            updated = True
            _update_retrospective_outcome(root, finding)

            try:
                diff_result = subprocess.run(
                    ["gh", "pr", "diff", str(pr_number)],
                    capture_output=True,
                    text=True,
                    timeout=runtime.gh_api_timeout,
                    cwd=str(root),
                )
                if diff_result.returncode == 0 and diff_result.stdout.strip():
                    runtime.save_fix_template(root, finding, diff_result.stdout)
            except (subprocess.TimeoutExpired, OSError):
                pass
            continue

        if state == "CLOSED":
            runtime.update_q_value(q_table, q_state, "attempt_fix", runtime.pr_feedback_reward_closed, None)
            finding["merge_outcome"] = "closed_unmerged"
            finding["q_reward_applied"] = True
            updated = True
            _update_retrospective_outcome(root, finding)

    if updated:
        runtime.save_autofix_q_table(root, q_table)

    return findings


def process_finding(
    finding: dict,
    root: Path,
    policy: dict | None,
    findings: list[dict] | None,
    runtime: ScannerRuntime,
) -> dict:
    """Route and process a single finding based on policy, confidence, and rate limits."""
    policy = policy or runtime.load_policy(root)
    findings = findings or []
    finding["attempt_count"] = finding.get("attempt_count", 0) + 1

    if finding["attempt_count"] > runtime.max_attempts:
        runtime.log(
            f"Finding {finding['finding_id']} failed {runtime.max_attempts} fix attempts, falling back to issue"
        )
        finding["rollout_mode"] = "issue-only"
        return runtime.open_github_issue(finding, root, policy)

    category = finding.get("category", "")
    existing_findings = runtime.load_findings(root)
    category_status, category_reason = runtime.check_category_health(category, existing_findings)
    if category_status == "disabled":
        finding["status"] = "failed"
        finding["fail_reason"] = f"category_disabled: {category_reason}"
        finding["processed_at"] = runtime.now_iso()
        runtime.log(f"Category '{category}' disabled: {category_reason}")
        return finding

    category_config = policy.get("categories", {}).get(category, runtime.default_category_policy(category))
    confidence = float(category_config.get("confidence", 0.0) or 0.0)
    finding["confidence_score"] = round(confidence, 3)

    suppression = runtime.suppression_reason(finding, policy)
    if suppression:
        finding["status"] = "suppressed-policy"
        finding["suppression_reason"] = suppression
        finding["processed_at"] = runtime.now_iso()
        return finding

    if not category_config.get("enabled", True) or category_config.get("mode") == "disabled":
        finding["status"] = "suppressed-policy"
        finding["suppression_reason"] = "category disabled by autofix policy"
        finding["processed_at"] = runtime.now_iso()
        return finding

    project_config = runtime.project_policy(root)
    q_learning_enabled = bool(project_config.get("repair_qlearning", False))
    q_table = None
    q_action = None
    q_state = None

    if q_learning_enabled:
        try:
            q_table = runtime.load_autofix_q_table(root)
            evidence_file = str(finding.get("evidence", {}).get("file", ""))
            file_ext = Path(evidence_file).suffix if evidence_file else ""
            severity = finding.get("severity", "medium")
            centrality_tier = runtime.compute_centrality_tier(evidence_file, root)
            q_state = runtime.encode_autofix_state(category, file_ext, centrality_tier, severity)
            q_action, q_source = runtime.select_action(
                q_table,
                q_state,
                ["attempt_fix", "open_issue", "skip"],
                epsilon=runtime.qlearn_epsilon,
            )
            runtime.log(
                f"Q-learning routing for {finding['finding_id']}: action={q_action} (source={q_source}, state={q_state})"
            )

            entries = q_table.get("entries", {})
            state_values = entries.get(q_state, {})
            has_learned = any(float(state_values.get(action, 0.0)) != 0.0 for action in ["attempt_fix", "open_issue", "skip"])
            if not has_learned:
                runtime.log(f"Q-learning: no learned values for {q_state}, falling through to fixability")
                q_action = None

            if q_action == "skip":
                finding["status"] = "suppressed-policy"
                finding["suppression_reason"] = "q-learning:skip"
                finding["processed_at"] = runtime.now_iso()
                reward = runtime.compute_autofix_reward(finding)
                runtime.update_q_value(q_table, q_state, q_action, reward, None)
                runtime.save_autofix_q_table(root, q_table)
                return finding

            if q_action == "open_issue":
                finding["rollout_mode"] = "issue-only"
                result = runtime.open_github_issue(finding, root, policy)
                reward = runtime.compute_autofix_reward(result)
                runtime.update_q_value(q_table, q_state, q_action, reward, None)
                runtime.save_autofix_q_table(root, q_table)
                return result
        except Exception as exc:
            runtime.log(f"Q-learning error, falling through to fixability: {exc}")
            q_table = None
            q_action = None
            q_state = None

    fixability = runtime.classify_fixability(finding)
    finding["fixability"] = fixability

    if category == "recurring-audit":
        finding["rollout_mode"] = "issue-only"
        result = runtime.open_github_issue(finding, root, policy)
        if q_learning_enabled and q_table is not None and q_state is not None:
            reward = runtime.compute_autofix_reward(result)
            runtime.update_q_value(q_table, q_state, q_action or "open_issue", reward, None)
            runtime.save_autofix_q_table(root, q_table)
        return result

    if (
        fixability == "review-only"
        or category_config.get("mode") == "issue-only"
        or confidence < float(category_config.get("min_confidence_autofix", runtime.min_conf_autofix) or runtime.min_conf_autofix)
    ):
        finding["rollout_mode"] = "issue-only"
        result = runtime.open_github_issue(finding, root, policy)
        if q_learning_enabled and q_table is not None and q_state is not None:
            reward = runtime.compute_autofix_reward(result)
            runtime.update_q_value(q_table, q_state, q_action or "open_issue", reward, None)
            runtime.save_autofix_q_table(root, q_table)
        return result

    rate_limit = runtime.rate_limit_reason(policy, findings)
    if rate_limit:
        finding["status"] = "rate-limited"
        finding["fail_reason"] = rate_limit
        finding["processed_at"] = runtime.now_iso()
        return finding

    finding["rollout_mode"] = "autofix"
    result = runtime.autofix_finding(finding, root, policy)
    if q_learning_enabled and q_table is not None and q_state is not None:
        reward = runtime.compute_autofix_reward(result)
        runtime.update_q_value(q_table, q_state, q_action or "attempt_fix", reward, None)
        runtime.save_autofix_q_table(root, q_table)

    return result


def scan_locked(root: Path, max_findings: int, runtime: ScannerRuntime) -> int:
    """Run the scanner lifecycle while the caller holds the lock."""
    start_time = time.monotonic()

    runtime.log(f"Starting proactive scan on {root}")
    policy = runtime.load_policy(root)
    runtime.cleanup_merged_branches(root)

    existing_findings = runtime.load_findings(root)
    if policy.get("pr_feedback_loop", True):
        try:
            existing_findings = check_pr_outcomes(root, existing_findings, runtime)
        except Exception as exc:
            runtime.log(f"PR outcome check failed (non-fatal): {exc}")

    existing_findings, metrics = sync_outcomes(root, existing_findings, policy, runtime)
    existing_findings = runtime.prune_findings(existing_findings)

    skipped_dedup = 0
    accepted_for_processing = 0
    raw_findings_count = 0

    summary_counts: dict[str, int] = {
        "processed": 0,
        "skipped_dedup": skipped_dedup,
        "fixed": 0,
        "issues_opened": 0,
        "failed": 0,
        "rate_limited": 0,
        "suppressed": 0,
    }
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    all_scan_findings: list[dict] = []

    def _record_result(result: dict) -> None:
        existing_findings.append(result)
        all_scan_findings.append(result)
        summary_counts["processed"] += 1
        status = result.get("status", "")
        if status == "fixed":
            summary_counts["fixed"] += 1
        elif status == "issue-opened":
            summary_counts["issues_opened"] += 1
        elif status == "rate-limited":
            summary_counts["rate_limited"] += 1
        elif status == "suppressed-policy":
            summary_counts["suppressed"] += 1
        elif status in ("failed", "permanently_failed"):
            summary_counts["failed"] += 1

    def _process_incoming_findings(raw_findings: list[dict]) -> None:
        nonlocal skipped_dedup, accepted_for_processing, raw_findings_count
        if not raw_findings:
            return
        raw_findings_count += len(raw_findings)

        to_process: list[dict] = []
        for finding in raw_findings:
            if accepted_for_processing >= max_findings:
                break
            skip_reason = runtime.dedup_finding(finding, existing_findings)
            if skip_reason:
                runtime.log(f"Skipping {finding['finding_id']}: {skip_reason}")
                finding["status"] = "skipped-dedup"
                finding["processed_at"] = runtime.now_iso()
                finding["fail_reason"] = skip_reason
                existing_findings.append(finding)
                skipped_dedup += 1
                summary_counts["skipped_dedup"] = skipped_dedup
                continue
            to_process.append(finding)
            accepted_for_processing += 1

        if not to_process:
            return

        runtime.log(
            f"Processing {len(to_process)} findings (max={max_findings}, skipped_dedup={skipped_dedup})"
        )

        batches: list[list[dict]] = []
        individual_findings: list[dict] = []
        if policy.get("batch_similar_findings", True) and len(to_process) >= runtime.batch_min_group_size:
            groups = group_similar_findings(to_process, runtime.batch_min_group_size)
            for group in groups:
                same_file_group = (
                    len(group) > 1
                    and all(
                        finding.get("category") == "llm-review"
                        and str(finding.get("evidence", {}).get("file", "") or "")
                        == str(group[0].get("evidence", {}).get("file", "") or "")
                        for finding in group
                    )
                )
                if len(group) >= runtime.batch_min_group_size or same_file_group:
                    batches.append(group)
                    continue
                individual_findings.extend(group)
            if batches:
                runtime.log(
                    f"Batch grouping: {len(batches)} batches, {len(individual_findings)} individual findings"
                )
        else:
            individual_findings = to_process

        for batch in batches:
            elapsed = time.monotonic() - start_time
            remaining = runtime.scan_timeout_seconds - elapsed
            if remaining < 120:
                runtime.log(f"Time budget low ({remaining:.0f}s), skipping remaining batches")
                for finding in batch:
                    finding["status"] = "new"
                    finding["fail_reason"] = "timeout_budget_exhausted"
                    finding["processed_at"] = runtime.now_iso()
                    existing_findings.append(finding)
                    all_scan_findings.append(finding)
                    summary_counts["skipped"] = summary_counts.get("skipped", 0) + 1
                continue

            for result in autofix_batch(batch, root, policy or {}, runtime):
                _record_result(result)

        for finding in individual_findings:
            elapsed = time.monotonic() - start_time
            remaining = runtime.scan_timeout_seconds - elapsed
            if remaining < 60:
                runtime.log(f"Time budget low ({remaining:.0f}s remaining), stopping processing")
                finding["status"] = "new"
                finding["fail_reason"] = "timeout_budget_exhausted"
                finding["processed_at"] = runtime.now_iso()
                existing_findings.append(finding)
                all_scan_findings.append(finding)
                summary_counts["skipped"] = summary_counts.get("skipped", 0) + 1
                continue

            processed = process_finding(finding, root, policy, existing_findings, runtime)
            if processed["status"] == "failed" and processed["attempt_count"] < runtime.max_attempts:
                runtime.log(
                    f"Finding {processed['finding_id']} failed attempt {processed['attempt_count']}, will retry next cycle"
                )
            _record_result(processed)

    _process_incoming_findings(runtime.detect_syntax_errors(root))
    _process_incoming_findings(runtime.detect_recurring_audit(root))
    _process_incoming_findings(runtime.detect_dependency_vulns(root))
    _process_incoming_findings(runtime.detect_dead_code(root))
    _process_incoming_findings(runtime.detect_architectural_drift(root))
    runtime.detect_llm_review(root, on_findings=_process_incoming_findings)
    runtime.log(f"Detected {raw_findings_count} raw findings")

    for finding in all_scan_findings:
        category = finding.get("category", "unknown")
        severity = finding.get("severity", "unknown")
        by_category[category] = by_category.get(category, 0) + 1
        by_severity[severity] = by_severity.get(severity, 0) + 1

    for finding in new_findings:
        if finding.get("status") == "skipped-dedup":
            category = finding.get("category", "unknown")
            severity = finding.get("severity", "unknown")
            by_category.setdefault(category, 0)
            by_severity.setdefault(severity, 0)

    runtime.save_findings(root, existing_findings)
    policy = runtime.recompute_category_confidence(policy)
    runtime.save_policy(root, policy)
    metrics = runtime.write_autofix_metrics(root, existing_findings, policy)
    benchmarks = runtime.build_autofix_benchmarks(root, existing_findings, policy)

    category_health: dict[str, dict] = {}
    for category in sorted(
        {str(finding.get("category", "")) for finding in existing_findings if finding.get("category")}
    ):
        status, reason = runtime.check_category_health(category, existing_findings)
        if status == "disabled":
            category_health[category] = {"status": status, "reason": reason}

    try:
        from autofix.platform import write_state_snapshot

        write_state_snapshot(
            root,
            "findings.json",
            {"findings": existing_findings, "category_health": category_health},
        )
    except OSError:
        pass

    haiku_invocations = 1 if any(finding.get("category") == "llm-review" for finding in new_findings) else 0
    fix_invocations = 0
    opus_fix_invocations = 0
    for finding in all_scan_findings:
        fixability = finding.get("fixability", "")
        if fixability in ("deterministic", "likely-safe"):
            fix_invocations += 1
            if finding.get("severity") in ("high", "critical"):
                opus_fix_invocations += 1

    default_fix_invocations = fix_invocations - opus_fix_invocations
    estimated_cost = (
        haiku_invocations * 0.03
        + default_fix_invocations * 0.50
        + opus_fix_invocations * 2.00
    )

    elapsed = time.monotonic() - start_time
    output = {
        "findings": all_scan_findings,
        "summary": {
            "by_category": by_category,
            "by_severity": by_severity,
            "processed": summary_counts["processed"],
            "skipped_dedup": summary_counts["skipped_dedup"],
            "fixed": summary_counts["fixed"],
            "issues_opened": summary_counts["issues_opened"],
            "failed": summary_counts["failed"],
            "rate_limited": summary_counts["rate_limited"],
            "suppressed": summary_counts["suppressed"],
        },
        "autofix_metrics": metrics,
        "autofix_benchmarks": benchmarks,
        "cost": {
            "haiku_invocations": haiku_invocations,
            "fix_invocations": fix_invocations,
            "estimated_cost_usd": round(estimated_cost, 2),
        },
        "scan_duration_seconds": round(elapsed, 2),
    }

    try:
        from autofix.platform import write_scan_artifact

        write_scan_artifact(
            root,
            "findings.json",
            {"findings": all_scan_findings, "category_health": category_health},
        )
        write_scan_artifact(root, "summary.json", output)
    except OSError:
        pass

    runtime.log_event(
        root,
        "autofix_scan",
        duration_s=round(time.monotonic() - start_time, 3),
        total_detected=len(new_findings),
        processed=summary_counts.get("processed", 0),
        fixed=summary_counts.get("fixed", 0),
        issues_opened=summary_counts.get("issues_opened", 0),
        failed=summary_counts.get("failed", 0),
        skipped_dedup=summary_counts.get("skipped_dedup", 0),
    )
    print(json.dumps(output, indent=2))
    return 0


def resolve_scan_args(args: argparse.Namespace) -> tuple[Path, int]:
    """Normalize argparse scan inputs for thin CLI wrappers."""
    return Path(args.root).resolve(), int(args.max_findings)
