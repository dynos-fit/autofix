"""Standalone CLI for the extracted autofix package."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autofix.platform import write_json
from autofix.scanner import resolve_scan_args, scan_locked
from autofix.state import (
    autofix_policy_path,
    build_autofix_benchmarks,
    findings_path,
    load_autofix_policy,
    load_findings,
    save_autofix_policy,
    save_findings,
)


def build_parser(*, scan_handler, sync_handler, runtime_factory) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone autofix scanner")
    sub = parser.add_subparsers(dest="subcommand")

    p_scan = sub.add_parser("scan", help="Run proactive scan")
    p_scan.add_argument("--root", default=".", help="Project root path")
    p_scan.add_argument("--max-findings", default=100, type=int, help="Max findings to process per cycle")
    p_scan.set_defaults(func=lambda args: scan_handler(args))

    p_list = sub.add_parser("list", help="List current findings")
    p_list.add_argument("--root", default=".", help="Project root path")
    p_list.set_defaults(func=cmd_list)

    p_clear = sub.add_parser("clear", help="Clear findings file")
    p_clear.add_argument("--root", default=".", help="Project root path")
    p_clear.set_defaults(func=cmd_clear)

    p_policy = sub.add_parser("policy", help="Show current autofix policy")
    p_policy.add_argument("--root", default=".", help="Project root path")
    p_policy.set_defaults(func=cmd_policy)

    p_sync = sub.add_parser("sync-outcomes", help="Refresh PR/issue outcomes and metrics")
    p_sync.add_argument("--root", default=".", help="Project root path")
    p_sync.set_defaults(func=lambda args: sync_handler(args))

    p_benchmark = sub.add_parser("benchmark", help="Build autofix benchmark summary from findings")
    p_benchmark.add_argument("--root", default=".", help="Project root path")
    p_benchmark.set_defaults(func=cmd_benchmark)

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
    p_suppress_list.set_defaults(func=cmd_suppress_list)

    p_suppress_remove = suppress_sub.add_parser("remove", help="Remove suppressions")
    p_suppress_remove.add_argument("--root", default=".", help="Project root path")
    p_suppress_remove.add_argument("--finding-id", default=None, help="Specific finding id to remove")
    p_suppress_remove.add_argument("--category", default=None, help="Category suppression to remove")
    p_suppress_remove.add_argument("--path-prefix", default=None, help="Path prefix suppression to remove")
    p_suppress_remove.set_defaults(func=cmd_suppress_remove)

    parser.set_defaults(runtime_factory=runtime_factory)
    return parser


def cmd_list(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    print(json.dumps(load_findings(root), indent=2))
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
    print(json.dumps(build_autofix_benchmarks(root, findings, policy), indent=2))
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
    print(json.dumps(load_autofix_policy(root).get("suppressions", []), indent=2))
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
    return scan_locked(root, max_findings, runtime_factory())


def standalone_sync_outcomes(args: argparse.Namespace, sync_outcomes_fn, runtime_factory) -> int:
    root = Path(args.root).resolve()
    findings = load_findings(root)
    policy = load_autofix_policy(root)
    findings, metrics = sync_outcomes_fn(root, findings, policy, runtime_factory())
    save_findings(root, findings)
    write_json(findings_path(root), {"findings": findings})
    print(json.dumps({"synced": True, "count": len(findings), "metrics": metrics}, indent=2))
    return 0

