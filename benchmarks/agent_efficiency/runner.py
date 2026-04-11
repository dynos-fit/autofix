#!/usr/bin/env python3
"""Standalone benchmark runner for coding-agent efficiency."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.agent_efficiency.metrics import summarize_task_reports


@dataclass
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    exact: bool


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, round(len(text) / 4))


def _load_usage(prompt_text: str, stdout: str, stderr: str, usage_path: Path) -> TokenUsage:
    if usage_path.exists():
        data = _load_json(usage_path)
        exact_flag = data.get("exact")
        exact = bool(exact_flag) if isinstance(exact_flag, bool) else True
        return TokenUsage(
            prompt_tokens=int(data.get("prompt_tokens", 0) or 0),
            completion_tokens=int(data.get("completion_tokens", 0) or 0),
            total_tokens=int(data.get("total_tokens", 0) or 0),
            exact=exact,
        )
    prompt_tokens = _estimate_tokens(prompt_text)
    completion_tokens = _estimate_tokens(stdout + "\n" + stderr)
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        exact=False,
    )


def _load_trace_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            events.append(item)
    return events


def _summarize_trace(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_name: dict[str, dict[str, Any]] = {}
    tool_call_totals: dict[str, int] = {}
    total_traced_tokens = 0
    exact_usage_events = 0
    llm_calls = 0
    tool_calls = 0

    for event in events:
        name = str(event.get("name", "unknown"))
        event_type = str(event.get("event_type", "") or "")
        if not event_type:
            if name.startswith("tool::"):
                event_type = "tool"
            elif name.startswith("llm"):
                event_type = "llm"
            elif isinstance(event.get("usage"), dict):
                event_type = "llm"

        bucket = by_name.setdefault(
            name,
            {
                "calls": 0,
                "failures": 0,
                "total_duration_seconds": 0.0,
                "total_tokens": 0,
            },
        )
        bucket["calls"] += 1
        bucket["total_duration_seconds"] += float(event.get("duration_seconds", 0.0) or 0.0)
        if not bool(event.get("ok", False)):
            bucket["failures"] += 1

        if event_type == "llm":
            llm_calls += 1
        elif event_type == "tool":
            tool_calls += 1
            tool_name = str(event.get("tool_name") or name.removeprefix("tool::") or name)
            tool_call_totals[tool_name] = tool_call_totals.get(tool_name, 0) + 1

        usage = event.get("usage")
        if isinstance(usage, dict):
            tokens = int(usage.get("total_tokens", 0) or 0)
            bucket["total_tokens"] += tokens
            total_traced_tokens += tokens
            exact_usage_events += 1

    return {
        "events": len(events),
        "exact_usage_events": exact_usage_events,
        "total_traced_tokens": total_traced_tokens,
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "steps": llm_calls,
        "tool_call_totals": tool_call_totals,
        "by_name": by_name,
    }


def _run(cmd: str, *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        shlex.split(cmd),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_baseline(workdir: Path) -> None:
    _run("git init", cwd=workdir, timeout=20)
    _run("git config user.name Benchmark Runner", cwd=workdir, timeout=20)
    _run("git config user.email benchmark@example.com", cwd=workdir, timeout=20)
    _run("git add -A", cwd=workdir, timeout=20)
    _run("git commit -m baseline", cwd=workdir, timeout=20)


def _diff_stats(workdir: Path) -> dict[str, int]:
    result = _run("git diff --numstat HEAD", cwd=workdir, timeout=20)
    files_changed = 0
    insertions = 0
    deletions = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        files_changed += 1
        try:
            insertions += int(parts[0])
        except ValueError:
            pass
        try:
            deletions += int(parts[1])
        except ValueError:
            pass
    return {
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
    }


def _diff_files(workdir: Path) -> list[str]:
    result = _run("git diff --name-only HEAD", cwd=workdir, timeout=20)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _in_scope(path: str, patterns: list[str]) -> bool:
    normalized = path.strip("/")
    for pattern in patterns:
        candidate = str(pattern or "").strip("/")
        if not candidate:
            continue
        if normalized == candidate or normalized.startswith(candidate + "/"):
            return True
    return False


def _scope_from_task(task: dict[str, Any]) -> dict[str, list[str]]:
    scope = task.get("scope", {})
    if not isinstance(scope, dict):
        return {"allowed_files": [], "forbidden_files": []}
    allowed = [str(item) for item in scope.get("allowed_files", []) if str(item).strip()]
    forbidden = [str(item) for item in scope.get("forbidden_files", []) if str(item).strip()]
    return {"allowed_files": allowed, "forbidden_files": forbidden}


def _scope_violations(task: dict[str, Any], files_touched: list[str]) -> list[str]:
    scope = _scope_from_task(task)
    allowed = scope["allowed_files"]
    forbidden = scope["forbidden_files"]
    violations: list[str] = []
    for path in files_touched:
        if forbidden and _in_scope(path, forbidden):
            violations.append(f"modified forbidden path: {path}")
            continue
        if allowed and not _in_scope(path, allowed):
            violations.append(f"modified out-of-scope path: {path}")
    return violations


def _run_verification(task: dict[str, Any], workdir: Path) -> tuple[bool, list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    success = True
    for check in task.get("verification", []):
        command = str(check["command"]).format(
            python_executable=sys.executable,
            workdir=str(workdir),
        )
        started = time.time()
        result = _run(command, cwd=workdir, timeout=120)
        duration = round(time.time() - started, 3)
        item = {
            "name": check.get("name", "check"),
            "command": command,
            "returncode": result.returncode,
            "duration_seconds": duration,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        results.append(item)
        if result.returncode != 0:
            success = False
    return success, results


def _build_prompt(task: dict[str, Any]) -> str:
    verification_lines = [
        f"- {str(item['command']).format(python_executable=sys.executable)}" for item in task.get("verification", [])
    ]
    scope = _scope_from_task(task)
    scope_lines: list[str] = []
    if scope["allowed_files"]:
        scope_lines.append("Allowed files: " + ", ".join(scope["allowed_files"]))
    if scope["forbidden_files"]:
        scope_lines.append("Forbidden files: " + ", ".join(scope["forbidden_files"]))
    scope_block = ("\nScope:\n" + "\n".join(f"- {line}" for line in scope_lines) + "\n") if scope_lines else ""
    return (
        f"Task ID: {task['id']}\n"
        f"Title: {task['title']}\n"
        f"Category: {task['category']}\n"
        f"Difficulty: {task['difficulty']}\n"
        + scope_block
        + "\nInstruction:\n"
        f"{task['instruction']}\n\n"
        "Verification commands:\n"
        + "\n".join(verification_lines)
        + "\n"
    )


def _format_command(
    template: str,
    *,
    repo_root: Path,
    task_id: str,
    task_file: Path,
    workdir: Path,
    prompt_file: Path,
    usage_file: Path,
    trace_file: Path,
) -> str:
    return template.format(
        repo_root=str(repo_root),
        python_executable=sys.executable,
        task_id=task_id,
        task_file=str(task_file),
        workdir=str(workdir),
        prompt_file=str(prompt_file),
        usage_file=str(usage_file),
        trace_file=str(trace_file),
    )


def _run_task(repo_root: Path, task_path: Path, adapter: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    task = _load_json(task_path)
    task_root = task_path.parent
    source_repo = task_root / str(task.get("repo_dir", "repo"))
    task_id = str(task["id"])

    with tempfile.TemporaryDirectory(prefix=f"agent-bench-{task_id}-") as tmp:
        tmp_dir = Path(tmp)
        workdir = tmp_dir / "workdir"
        shutil.copytree(source_repo, workdir)
        _git_baseline(workdir)

        tests_passed_before, verification_before = _run_verification(task, workdir)

        prompt_text = _build_prompt(task)
        prompt_file = tmp_dir / "prompt.txt"
        usage_file = tmp_dir / "usage.json"
        trace_file = tmp_dir / "trace.jsonl"
        prompt_file.write_text(prompt_text, encoding="utf-8")

        command = _format_command(
            str(adapter["command_template"]),
            repo_root=repo_root,
            task_id=task_id,
            task_file=task_path,
            workdir=workdir,
            prompt_file=prompt_file,
            usage_file=usage_file,
            trace_file=trace_file,
        )

        started = time.time()
        result = _run(command, cwd=repo_root, timeout=600)
        wall_time = round(time.time() - started, 3)

        tests_passed_after, verification_after = _run_verification(task, workdir)
        usage = _load_usage(prompt_text, result.stdout, result.stderr, usage_file)
        trace_events = _load_trace_events(trace_file)
        trace_summary = _summarize_trace(trace_events)
        diff = _diff_stats(workdir)
        files_touched = _diff_files(workdir)
        scope = _scope_from_task(task)
        scope_violations = _scope_violations(task, files_touched)

        report = {
            "task_id": task_id,
            "title": task["title"],
            "category": task["category"],
            "difficulty": task["difficulty"],
            "agent_name": adapter["name"],
            "model_hint": str(adapter.get("model_hint", "") or ""),
            "success": bool(result.returncode == 0 and tests_passed_after and not scope_violations),
            "agent_returncode": result.returncode,
            "wall_time_seconds": wall_time,
            "steps": int(trace_summary.get("steps", 0) or 0),
            "token_usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "exact": usage.exact,
            },
            "tests_passed_before": tests_passed_before,
            "tests_passed_after": tests_passed_after,
            "verification_before": verification_before,
            "verification_after": verification_after,
            "verification": verification_after,
            "scope": scope,
            "scope_violations": scope_violations,
            "files_touched": files_touched,
            "diff": diff,
            "trace": trace_summary,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "command": command,
        }

        task_report_path = output_dir / "tasks" / f"{task_id}.json"
        task_report_path.parent.mkdir(parents=True, exist_ok=True)
        task_report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite",
        default="benchmarks/agent_efficiency/suite.json",
        help="Path to the benchmark suite manifest",
    )
    parser.add_argument(
        "--adapter-config",
        required=True,
        help="JSON file with adapter name and command_template",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for benchmark reports",
    )
    args = parser.parse_args()

    repo_root = REPO_ROOT
    suite_path = (repo_root / args.suite).resolve()
    adapter_path = Path(args.adapter_config).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    os.chdir(repo_root)

    suite = _load_json(suite_path)
    adapter = _load_json(adapter_path)
    task_reports = []
    for task_ref in suite.get("tasks", []):
        task_path = (suite_path.parent / task_ref).resolve()
        task_reports.append(_run_task(repo_root, task_path, adapter, output_dir))

    aggregate = summarize_task_reports(
        task_reports,
        model_hint=str(adapter.get("model_hint", "") or ""),
        pricing=adapter.get("pricing"),
    ).to_dict()
    summary = {
        "suite_name": suite.get("name", "unknown"),
        "adapter_name": adapter.get("name", "unknown"),
        "model_hint": str(adapter.get("model_hint", "") or ""),
        "aggregate": aggregate,
        "tasks": task_reports,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [
        f"Suite: {summary['suite_name']}",
        f"Adapter: {summary['adapter_name']}",
        f"Tasks run: {summary['aggregate']['tasks_run']}",
        f"Tasks solved: {summary['aggregate']['tasks_solved']}",
        f"Success rate: {summary['aggregate']['success_rate']}",
        f"Invalid fixtures: {summary['aggregate']['invalid_fixtures']}",
        f"Exact token coverage: {summary['aggregate']['exact_token_coverage']}",
        f"Average total tokens: {summary['aggregate']['avg_total_tokens']}",
        f"Median total tokens: {summary['aggregate']['median_total_tokens']}",
        f"Max total tokens: {summary['aggregate']['max_total_tokens']}",
        f"Token snowball index: {summary['aggregate']['token_snowball_index']}",
        f"Expensive failure ratio: {summary['aggregate']['expensive_failure_ratio']}",
        f"Scope discipline: {summary['aggregate']['scope_discipline']}",
        f"Mean steps: {summary['aggregate']['mean_steps']}",
        f"Average wall time (s): {summary['aggregate']['avg_wall_time_seconds']}",
        f"Solved per 1k tokens: {summary['aggregate']['solved_per_1k_tokens']}",
        f"Solved per minute: {summary['aggregate']['solved_per_minute']}",
        f"Resource AUC: {summary['aggregate']['resource_auc']}",
        f"Total cost (USD): {summary['aggregate']['total_cost_usd']}",
        f"Cost per resolved task (USD): {summary['aggregate']['cost_per_resolved_task']}",
    ]
    if summary["aggregate"]["tool_call_totals"]:
        parts = ", ".join(
            f"{name}={count}" for name, count in sorted(summary["aggregate"]["tool_call_totals"].items())
        )
        lines.append(f"Tool calls: {parts}")
    (output_dir / "summary.md").write_text(
        "# Benchmark Summary\n\n" + "\n".join(f"- {line}" for line in lines) + "\n",
        encoding="utf-8",
    )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
