"""Standalone application wiring for the extracted autofix subsystem."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from autofix.cli import build_parser, standalone_scan, standalone_sync_outcomes
from autofix.config import config_show, config_set, resolve_config
from autofix.daemon import daemon_start, daemon_stop, daemon_status
from autofix.defaults import (
    BATCH_MIN_GROUP_SIZE,
    GH_API_TIMEOUT,
    LLM_INVOCATION_TIMEOUT,
    LLM_REVIEW_CHUNK_LINES,
    LLM_REVIEW_FILE_TRUNCATION,
    MAX_ATTEMPTS,
    MIN_CONF_AUTOFIX,
    PR_FEEDBACK_REWARD_CLOSED,
    PR_FEEDBACK_REWARD_MERGED,
    QLEARN_EPSILON,
    SCAN_TIMEOUT_SECONDS,
)
from autofix.llm_backend import LLMBackendConfig
from autofix.detectors import (
    detect_architectural_drift,
    detect_dead_code,
    detect_dependency_vulns,
    detect_llm_review,
    detect_recurring_audit,
    detect_syntax_errors,
)
from autofix.backend import create_dynos_backend
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
from autofix.init import cmd_init
from autofix.repo import repo_add, repo_remove, repo_list
from autofix.scan_all import cmd_scan_all
from autofix.scanner import ScannerRuntime, run_scan_with_lock, sync_outcomes
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
from autofix.platform import new_scan_id, write_scan_artifact


def _log(msg: str) -> None:
    print(f"[autofix] {msg}", flush=True, file=__import__("sys").stderr)


def runtime_factory(root: Path | None = None) -> ScannerRuntime:
    cfg = resolve_config(root) if root else {}
    backend_config = LLMBackendConfig(
        backend=str(cfg.get("llm_backend", "claude_cli") or "claude_cli"),
        base_url=str(cfg.get("llm_base_url", "") or ""),
        api_key=str(cfg.get("llm_api_key", "") or ""),
    )
    review_model = str(cfg.get("review_model", "default") or "default")
    backend = create_dynos_backend(
        load_policy=load_autofix_policy,
        log=_log,
        subprocess_module=subprocess,
        shutil_module=shutil,
        build_import_graph_fn=build_import_graph,
        get_neighbor_file_contents_fn=get_neighbor_file_contents,
        find_matching_template_fn=find_matching_template,
        llm_backend_config=backend_config,
        review_model=review_model,
        fix_model=str(cfg.get("fix_model", review_model) or review_model),
        llm_timeout=int(cfg.get("llm_timeout", LLM_INVOCATION_TIMEOUT)),
        llm_max_steps=int(cfg.get("llm_max_steps", 12)),
        review_file_truncation=int(cfg.get("review_file_truncation", LLM_REVIEW_FILE_TRUNCATION)),
        fix_surrounding_lines=int(cfg.get("fix_surrounding_lines", 20)),
        fix_neighbor_files=int(cfg.get("fix_neighbor_files", 5)),
        fix_neighbor_lines=int(cfg.get("fix_neighbor_lines", 100)),
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
        detect_llm_review=lambda root, **kwargs: detect_llm_review(
            root,
            log=_log,
            backend_config=backend_config,
            review_model=review_model,
            llm_timeout=int(cfg.get("llm_timeout", LLM_INVOCATION_TIMEOUT)),
            llm_max_steps=int(cfg.get("llm_max_steps", 12)),
            review_chunk_lines=int(cfg.get("review_chunk_lines", LLM_REVIEW_CHUNK_LINES)),
            review_file_truncation=int(cfg.get("review_file_truncation", LLM_REVIEW_FILE_TRUNCATION)),
            **kwargs,
        ),
        autofix_finding=backend.autofix_finding,
        open_github_issue=backend.open_github_issue,
        encode_autofix_state=encode_autofix_state,
        load_autofix_q_table=load_autofix_q_table,
        save_autofix_q_table=save_autofix_q_table,
        select_action=select_action,
        update_q_value=update_q_value,
        save_fix_template=save_fix_template,
        gh_api_timeout=GH_API_TIMEOUT,
        max_attempts=MAX_ATTEMPTS,
        min_conf_autofix=float(cfg.get("min_confidence", MIN_CONF_AUTOFIX)),
        scan_timeout_seconds=int(cfg.get("scan_timeout", SCAN_TIMEOUT_SECONDS)),
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
    cfg = resolve_config(root)
    llm_backend = str(cfg.get("llm_backend", "claude_cli") or "claude_cli")
    if llm_backend == "claude_cli" and not args.dry_run and not shutil.which("claude"):
        print(json.dumps({"ok": False, "error": "claude CLI not found"}))
        return 1
    try:
        scan_id = new_scan_id()
        started_at = now_iso()
        os.environ["AUTOFIX_SCAN_ID"] = scan_id
        write_scan_artifact(
            root,
            "manifest.json",
            {
                "scan_id": scan_id,
                "root": str(root),
                "started_at": started_at,
                "completed": False,
                "dry_run": bool(args.dry_run),
                "max_findings": int(args.max_findings),
            },
        )
        if args.dry_run:
            os.environ["AUTOFIX_DRY_RUN"] = "1"
        result = run_scan_with_lock(root, int(args.max_findings), runtime_factory(root=root))
        write_scan_artifact(
            root,
            "manifest.json",
            {
                "scan_id": scan_id,
                "root": str(root),
                "started_at": started_at,
                "completed": True,
                "completed_at": now_iso(),
                "dry_run": bool(args.dry_run),
                "max_findings": int(args.max_findings),
                "exit_code": result,
            },
        )
        return result
    except RuntimeError:
        print(json.dumps({"error": "scan already running"}))
        return 1
    finally:
        if args.dry_run:
            os.environ.pop("AUTOFIX_DRY_RUN", None)
        os.environ.pop("AUTOFIX_SCAN_ID", None)


def cmd_sync_outcomes(args: argparse.Namespace) -> int:
    return standalone_sync_outcomes(args, sync_outcomes, runtime_factory)


# ---------------------------------------------------------------------------
# New subcommand handlers
# ---------------------------------------------------------------------------


def handle_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    result = cmd_init(
        root,
        max_files=getattr(args, "max_files", None),
        interval=getattr(args, "interval", None),
    )
    print(result.message)
    return result.exit_code


def handle_daemon_start(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    result = daemon_start(root=root, interval=getattr(args, "interval", None))
    print(result.message)
    return result.exit_code


def handle_daemon_stop(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    result = daemon_stop(root=root)
    print(result.message)
    return result.exit_code


def handle_daemon_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    result = daemon_status(root=root)
    print(result.message)
    return result.exit_code


def handle_repo_add(args: argparse.Namespace) -> int:
    path = Path(args.path)
    home_dir = Path.home()
    result = repo_add(path, home_dir)
    print(result.message)
    return result.exit_code


def handle_repo_remove(args: argparse.Namespace) -> int:
    path = Path(args.path)
    home_dir = Path.home()
    result = repo_remove(path, home_dir)
    print(result.message)
    return result.exit_code


def handle_repo_list(args: argparse.Namespace) -> int:
    home_dir = Path.home()
    result = repo_list(home_dir)
    as_json = getattr(args, "as_json", False)
    if as_json:
        # Parse the output lines back into structured data for JSON
        lines = [l for l in result.output.strip().splitlines() if l.strip()] if result.output.strip() else []
        repos = [{"path": line} for line in lines]
        print(json.dumps(repos, indent=2))
    else:
        if result.output:
            print(result.output)
        else:
            print(result.message)
    return result.exit_code


def handle_config_show(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    as_json = getattr(args, "as_json", False)
    result = config_show(root, as_json=as_json)
    if result.output:
        print(result.output)
    if result.message:
        print(result.message, file=__import__("sys").stderr)
    return result.exit_code


def handle_config_set(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    result = config_set(root, args.key, args.value)
    if result.message:
        print(result.message)
    return result.exit_code


def handle_scan_all(args: argparse.Namespace) -> int:
    home_dir = Path.home()
    result = cmd_scan_all(home_dir=home_dir)
    as_json = getattr(args, "as_json", False)
    if as_json:
        print(json.dumps({"exit_code": result.exit_code, "output": result.output}, indent=2))
    else:
        print(result.output)
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        scan_handler=cmd_scan,
        sync_handler=cmd_sync_outcomes,
        runtime_factory=runtime_factory,
        init_handler=handle_init,
        daemon_start_handler=handle_daemon_start,
        daemon_stop_handler=handle_daemon_stop,
        daemon_status_handler=handle_daemon_status,
        repo_add_handler=handle_repo_add,
        repo_remove_handler=handle_repo_remove,
        repo_list_handler=handle_repo_list,
        config_show_handler=handle_config_show,
        config_set_handler=handle_config_set,
        scan_all_handler=handle_scan_all,
    )
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return func(args)
