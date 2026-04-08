"""Standalone application wiring for the extracted autofix subsystem."""

from __future__ import annotations

import argparse
import fcntl
import json
import shutil
import subprocess
from pathlib import Path

from autofix.cli import build_parser, standalone_scan, standalone_sync_outcomes
from autofix.defaults import (
    BATCH_MIN_GROUP_SIZE,
    GH_API_TIMEOUT,
    MAX_ATTEMPTS,
    MIN_CONF_AUTOFIX,
    PR_FEEDBACK_REWARD_CLOSED,
    PR_FEEDBACK_REWARD_MERGED,
    QLEARN_EPSILON,
    SCAN_TIMEOUT_SECONDS,
)
from autofix.detectors import (
    detect_architectural_drift,
    detect_dead_code,
    detect_dependency_vulns,
    detect_llm_review,
    detect_recurring_audit,
    detect_syntax_errors,
)
from autofix.dynos_backend import create_dynos_backend
from autofix.routing import check_category_health, compute_autofix_reward, compute_centrality_tier
from autofix.runtime.core import write_json
from autofix.runtime.dynos import (
    encode_autofix_state,
    find_matching_template,
    get_neighbor_file_contents,
    load_autofix_q_table,
    log_event,
    project_policy,
    save_autofix_q_table,
    save_fix_template,
    select_action,
    update_q_value,
)
from autofix.scanner import ScannerRuntime, sync_outcomes
from autofix.state import (
    build_autofix_benchmarks,
    default_category_policy,
    dedup_finding,
    findings_path,
    load_autofix_policy,
    load_findings,
    prune_findings,
    rate_limit_reason,
    recompute_category_confidence,
    save_autofix_policy,
    save_findings,
    suppression_reason,
    write_autofix_metrics,
)
from autofix.runtime.core import build_import_graph, now_iso


def _log(msg: str) -> None:
    print(f"[autofix] {msg}", flush=True, file=__import__("sys").stderr)


def _cleanup_merged_branches(root: Path) -> None:
    if not shutil.which("gh") or not shutil.which("git"):
        return
    try:
        result = subprocess.run(
            ["git", "branch", "-r"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(root),
        )
    except (subprocess.TimeoutExpired, OSError):
        return
    if result.returncode != 0:
        return

    remote_branches = [
        line.strip()
        for line in result.stdout.splitlines()
        if "dynos/auto-fix-" in line.strip()
    ]
    for remote_ref in remote_branches:
        branch_name = remote_ref.replace("origin/", "", 1)
        try:
            pr_result = subprocess.run(
                ["gh", "pr", "list", "--search", f"{branch_name} in:head", "--state", "merged", "--json", "number"],
                capture_output=True,
                text=True,
                timeout=GH_API_TIMEOUT,
                cwd=str(root),
            )
            if pr_result.returncode != 0:
                continue
        except (subprocess.TimeoutExpired, OSError):
            continue


def runtime_factory() -> ScannerRuntime:
    backend = create_dynos_backend(
        load_policy=load_autofix_policy,
        log=_log,
        subprocess_module=subprocess,
        shutil_module=shutil,
        build_import_graph_fn=build_import_graph,
        get_neighbor_file_contents_fn=get_neighbor_file_contents,
        find_matching_template_fn=find_matching_template,
    )
    return ScannerRuntime(
        log=_log,
        now_iso=now_iso,
        log_event=log_event,
        project_policy=project_policy,
        write_json=write_json,
        findings_path=findings_path,
        load_policy=load_autofix_policy,
        save_policy=save_autofix_policy,
        load_findings=load_findings,
        save_findings=save_findings,
        prune_findings=prune_findings,
        dedup_finding=dedup_finding,
        suppression_reason=suppression_reason,
        rate_limit_reason=rate_limit_reason,
        recompute_category_confidence=recompute_category_confidence,
        write_autofix_metrics=write_autofix_metrics,
        build_autofix_benchmarks=build_autofix_benchmarks,
        check_category_health=check_category_health,
        compute_centrality_tier=compute_centrality_tier,
        compute_autofix_reward=compute_autofix_reward,
        default_category_policy=default_category_policy,
        classify_fixability=_classify_fixability,
        detect_syntax_errors=detect_syntax_errors,
        detect_recurring_audit=detect_recurring_audit,
        detect_dependency_vulns=lambda root: detect_dependency_vulns(root, log=_log),
        detect_dead_code=detect_dead_code,
        detect_architectural_drift=lambda root: detect_architectural_drift(root, log=_log),
        detect_llm_review=lambda root: detect_llm_review(root, log=_log),
        autofix_finding=backend.autofix_finding,
        open_github_issue=backend.open_github_issue,
        cleanup_merged_branches=_cleanup_merged_branches,
        encode_autofix_state=encode_autofix_state,
        load_autofix_q_table=load_autofix_q_table,
        save_autofix_q_table=save_autofix_q_table,
        select_action=select_action,
        update_q_value=update_q_value,
        save_fix_template=save_fix_template,
        gh_api_timeout=GH_API_TIMEOUT,
        max_attempts=MAX_ATTEMPTS,
        min_conf_autofix=MIN_CONF_AUTOFIX,
        scan_timeout_seconds=SCAN_TIMEOUT_SECONDS,
        batch_min_group_size=BATCH_MIN_GROUP_SIZE,
        qlearn_epsilon=QLEARN_EPSILON,
        pr_feedback_reward_merged=PR_FEEDBACK_REWARD_MERGED,
        pr_feedback_reward_closed=PR_FEEDBACK_REWARD_CLOSED,
    )


def _classify_fixability(finding: dict) -> str:
    category = finding.get("category", "")
    if category == "syntax-error":
        return "deterministic"
    if category == "dead-code":
        evidence = finding.get("evidence", {})
        if evidence.get("unused_imports"):
            return "deterministic"
        return "likely-safe"
    if category == "llm-review":
        return "likely-safe"
    return "review-only"


def cmd_scan(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if not shutil.which("claude"):
        print(json.dumps({"ok": False, "error": "claude CLI not found"}))
        return 1
    lock_path = root / ".autofix" / "scan.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"error": "scan already running"}))
        lock_fd.close()
        return 1
    try:
        return standalone_scan(args, runtime_factory)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def cmd_sync_outcomes(args: argparse.Namespace) -> int:
    return standalone_sync_outcomes(args, sync_outcomes, runtime_factory)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(scan_handler=cmd_scan, sync_handler=cmd_sync_outcomes, runtime_factory=runtime_factory)
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return func(args)
