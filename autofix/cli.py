"""Standalone CLI for the extracted autofix package."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autofix.output import (
    format_benchmarks,
    format_findings,
    format_suppressions,
)
from autofix.platform import write_json
from autofix.scanner import resolve_scan_args, scan_locked
from autofix.state import (
    build_autofix_benchmarks,
    findings_path,
    load_autofix_policy,
    load_findings,
    save_autofix_policy,
    save_findings,
)


def build_parser(
    *,
    scan_handler,
    sync_handler,
    runtime_factory,
    init_handler=None,
    daemon_start_handler=None,
    daemon_stop_handler=None,
    daemon_status_handler=None,
    repo_add_handler=None,
    repo_remove_handler=None,
    repo_list_handler=None,
    config_show_handler=None,
    config_set_handler=None,
    scan_all_handler=None,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone autofix scanner")
    sub = parser.add_subparsers(dest="subcommand")

    # --- scan ---
    p_scan = sub.add_parser("scan", help="Run proactive scan")
    p_scan.add_argument("--root", default=".", help="Project root path")
    p_scan.add_argument("--max-findings", default=100, type=int, help="Max findings to process per cycle")
    p_scan.add_argument("--dry-run", action="store_true", help="Scan and route findings without opening issues or PRs")
    p_scan.set_defaults(func=lambda args: scan_handler(args))

    # --- list ---
    p_list = sub.add_parser("list", help="List current findings")
    p_list.add_argument("--root", default=".", help="Project root path")
    p_list.add_argument("--json", dest="as_json", action="store_true", default=False, help="Output as JSON")
    p_list.set_defaults(func=cmd_list)

    # --- clear ---
    p_clear = sub.add_parser("clear", help="Clear findings file")
    p_clear.add_argument("--root", default=".", help="Project root path")
    p_clear.set_defaults(func=cmd_clear)

    # --- policy ---
    p_policy = sub.add_parser("policy", help="Show current autofix policy")
    p_policy.add_argument("--root", default=".", help="Project root path")
    p_policy.set_defaults(func=cmd_policy)

    # --- sync-outcomes ---
    p_sync = sub.add_parser("sync-outcomes", help="Refresh PR/issue outcomes and metrics")
    p_sync.add_argument("--root", default=".", help="Project root path")
    p_sync.set_defaults(func=lambda args: sync_handler(args))

    # --- benchmark ---
    p_benchmark = sub.add_parser("benchmark", help="Build autofix benchmark summary from findings")
    p_benchmark.add_argument("--root", default=".", help="Project root path")
    p_benchmark.add_argument("--json", dest="as_json", action="store_true", default=False, help="Output as JSON")
    p_benchmark.set_defaults(func=cmd_benchmark)

    # --- suppress ---
    p_suppress = sub.add_parser("suppress", help="Manage autofix suppressions")
    suppress_sub = p_suppress.add_subparsers(dest="suppress_command", required=True)

    p_suppress_add = suppress_sub.add_parser("add", help="Add a suppression rule")
    p_suppress_add.add_argument("--root", default=".", help="Project root path")
    p_suppress_add.add_argument("--finding-id", default=None, help="Specific finding id to suppress")
    p_suppress_add.add_argument("--category", default=None, help="Category to suppress")
    p_suppress_add.add_argument("--path-prefix", default=None, help="Path prefix to suppress")
    p_suppress_add.add_argument("--days", type=int, default=30, help="Suppression duration in days")
    p_suppress_add.add_argument("--reason", default="manual suppression", help="Why this suppression exists")
    p_suppress_add.set_defaults(func=cmd_suppress_add)

    p_suppress_list = suppress_sub.add_parser("list", help="List suppressions")
    p_suppress_list.add_argument("--root", default=".", help="Project root path")
    p_suppress_list.add_argument("--json", dest="as_json", action="store_true", default=False, help="Output as JSON")
    p_suppress_list.set_defaults(func=cmd_suppress_list)

    p_suppress_remove = suppress_sub.add_parser("remove", help="Remove suppressions")
    p_suppress_remove.add_argument("--root", default=".", help="Project root path")
    p_suppress_remove.add_argument("--finding-id", default=None, help="Specific finding id to remove")
    p_suppress_remove.add_argument("--category", default=None, help="Category suppression to remove")
    p_suppress_remove.add_argument("--path-prefix", default=None, help="Path prefix suppression to remove")
    p_suppress_remove.set_defaults(func=cmd_suppress_remove)

    # --- init ---
    if init_handler is not None:
        p_init = sub.add_parser("init", help="Initialize autofix for a repository")
        p_init.add_argument("--root", default=".", help="Project root path")
        p_init.add_argument("--max-files", type=int, default=None, help="Per-repo max files override")
        p_init.add_argument("--interval", default=None, help="Per-repo scan interval (e.g. 15m, 2h)")
        p_init.set_defaults(func=init_handler)

    # --- daemon ---
    if daemon_start_handler is not None:
        p_daemon = sub.add_parser("daemon", help="Manage the autofix background daemon")
        daemon_sub = p_daemon.add_subparsers(dest="daemon_command", required=True)

        p_daemon_start = daemon_sub.add_parser("start", help="Start the daemon")
        p_daemon_start.add_argument("--root", default=".", help="Project root path")
        p_daemon_start.add_argument("--interval", default=None, help="Scan interval (e.g. 15m, 2h)")
        p_daemon_start.set_defaults(func=daemon_start_handler)

        p_daemon_stop = daemon_sub.add_parser("stop", help="Stop the daemon")
        p_daemon_stop.add_argument("--root", default=".", help="Project root path")
        p_daemon_stop.set_defaults(func=daemon_stop_handler)

        p_daemon_status = daemon_sub.add_parser("status", help="Show daemon status")
        p_daemon_status.add_argument("--root", default=".", help="Project root path")
        p_daemon_status.set_defaults(func=daemon_status_handler)

    # --- repo ---
    if repo_add_handler is not None:
        p_repo = sub.add_parser("repo", help="Manage registered repositories")
        repo_sub = p_repo.add_subparsers(dest="repo_command", required=True)

        p_repo_add = repo_sub.add_parser("add", help="Register a repository")
        p_repo_add.add_argument("path", help="Path to the repository")
        p_repo_add.set_defaults(func=repo_add_handler)

        p_repo_remove = repo_sub.add_parser("remove", help="Remove a repository")
        p_repo_remove.add_argument("path", help="Path to the repository")
        p_repo_remove.set_defaults(func=repo_remove_handler)

        p_repo_list = repo_sub.add_parser("list", help="List registered repositories")
        p_repo_list.add_argument("--json", dest="as_json", action="store_true", default=False, help="Output as JSON")
        p_repo_list.set_defaults(func=repo_list_handler)

    # --- config ---
    if config_show_handler is not None:
        p_config = sub.add_parser("config", help="Manage configuration")
        config_sub = p_config.add_subparsers(dest="config_command", required=True)

        p_config_show = config_sub.add_parser("show", help="Show resolved configuration")
        p_config_show.add_argument("--root", default=".", help="Project root path")
        p_config_show.add_argument("--json", dest="as_json", action="store_true", default=False, help="Output as JSON")
        p_config_show.set_defaults(func=config_show_handler)

        p_config_set = config_sub.add_parser("set", help="Set a configuration key")
        p_config_set.add_argument("--root", default=".", help="Project root path")
        p_config_set.add_argument("key", help="Configuration key")
        p_config_set.add_argument("value", help="Configuration value")
        p_config_set.set_defaults(func=config_set_handler)

    # --- scan-all ---
    if scan_all_handler is not None:
        p_scan_all = sub.add_parser("scan-all", help="Scan all registered repositories")
        p_scan_all.add_argument("--json", dest="as_json", action="store_true", default=False, help="Output as JSON")
        p_scan_all.set_defaults(func=scan_all_handler)

    parser.set_defaults(runtime_factory=runtime_factory)
    return parser


def cmd_list(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    findings = load_findings(root)
    as_json = getattr(args, "as_json", False)
    print(format_findings(findings, as_json=as_json))
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    path = findings_path(root)
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        print(json.dumps({"cleared": False, "error": str(exc)}))
        return 1
    print(json.dumps({"cleared": True}))
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    print(json.dumps(load_autofix_policy(root), indent=2))
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    findings = load_findings(root)
    policy = load_autofix_policy(root)
    benchmarks = build_autofix_benchmarks(root, findings, policy)
    as_json = getattr(args, "as_json", False)
    print(format_benchmarks(benchmarks, as_json=as_json))
    return 0


def cmd_suppress_add(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    policy = load_autofix_policy(root)
    entry = {
        "finding_id": args.finding_id,
        "category": args.category,
        "path_prefix": args.path_prefix,
        "until": (
            datetime.now(timezone.utc) + timedelta(days=int(args.days))
        ).isoformat() if args.days else None,
        "reason": args.reason or "manual suppression",
    }
    policy.setdefault("suppressions", []).append(entry)
    save_autofix_policy(root, policy)
    print(json.dumps({"added": entry}, indent=2))
    return 0


def cmd_suppress_list(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    suppressions = load_autofix_policy(root).get("suppressions", [])
    as_json = getattr(args, "as_json", False)
    print(format_suppressions(suppressions, as_json=as_json))
    return 0


def cmd_suppress_remove(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    policy = load_autofix_policy(root)
    before = list(policy.get("suppressions", []))
    remaining = []
    for entry in before:
        if args.finding_id and entry.get("finding_id") == args.finding_id:
            continue
        if args.category and entry.get("category") == args.category:
            continue
        if args.path_prefix and entry.get("path_prefix") == args.path_prefix:
            continue
        remaining.append(entry)
    policy["suppressions"] = remaining
    save_autofix_policy(root, policy)
    print(json.dumps({"removed": len(before) - len(remaining), "remaining": remaining}, indent=2))
    return 0


def standalone_scan(args: argparse.Namespace, runtime_factory) -> int:
    root, max_findings = resolve_scan_args(args)
    return scan_locked(root, max_findings, runtime_factory(root=root))


def standalone_sync_outcomes(args: argparse.Namespace, sync_outcomes_fn, runtime_factory) -> int:
    root = Path(args.root).resolve()
    findings = load_findings(root)
    policy = load_autofix_policy(root)
    findings, metrics = sync_outcomes_fn(root, findings, policy, runtime_factory(root=root))
    save_findings(root, findings)
    write_json(findings_path(root), {"findings": findings})
    print(json.dumps({"synced": True, "count": len(findings), "metrics": metrics}, indent=2))
    return 0
