#!/usr/bin/env python3
"""Zero-touch adapter that benchmarks the real autofix review/fix loops."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autofix.agent_loop import run_agent_loop, run_review_agent_loop
from autofix.llm_backend import LLMBackendConfig
from benchmarks.agent_efficiency.instrumentation.core import read_trace_events


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, round(len(text) / 4))


def _write_event(trace_file: Path, event: dict[str, Any]) -> None:
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    with trace_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _scope(task: dict[str, Any]) -> dict[str, list[str]]:
    scope = task.get("scope", {})
    if not isinstance(scope, dict):
        return {"allowed_files": [], "forbidden_files": []}
    return {
        "allowed_files": [str(item) for item in scope.get("allowed_files", []) if str(item).strip()],
        "forbidden_files": [str(item) for item in scope.get("forbidden_files", []) if str(item).strip()],
    }


def _parse_findings(raw: str) -> list[dict[str, Any]]:
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _in_scope(path: str, patterns: list[str]) -> bool:
    normalized = path.strip("/")
    for pattern in patterns:
        candidate = str(pattern or "").strip("/")
        if not candidate:
            continue
        if normalized == candidate or normalized.startswith(candidate + "/"):
            return True
    return False


def _filter_findings(findings: list[dict[str, Any]], allowed_files: list[str]) -> list[dict[str, Any]]:
    if not allowed_files:
        return findings
    filtered: list[dict[str, Any]] = []
    for finding in findings:
        file_path = str(finding.get("file") or "")
        if not file_path or _in_scope(file_path, allowed_files):
            filtered.append(finding)
    return filtered


def _fallback_file_hint(task: dict[str, Any], scope: dict[str, list[str]]) -> str:
    if scope["allowed_files"]:
        return scope["allowed_files"][0]
    instruction = str(task.get("instruction", ""))
    for token in instruction.replace("`", " ").split():
        if "/" in token and "." in token:
            return token.strip(".,:;")
    return ""


def _verification_block(task: dict[str, Any]) -> str:
    commands = [str(item.get("command", "")).strip() for item in task.get("verification", []) if str(item.get("command", "")).strip()]
    if not commands:
        return ""
    return "Verification commands:\n" + "\n".join(f"- {command}" for command in commands)


def _scope_block(scope: dict[str, list[str]]) -> str:
    lines: list[str] = []
    if scope["allowed_files"]:
        lines.append("Only modify: " + ", ".join(scope["allowed_files"]))
    if scope["forbidden_files"]:
        lines.append("Do not modify: " + ", ".join(scope["forbidden_files"]))
    return "\n".join(lines)


def _install_trace_patches(trace_file: Path) -> None:
    from autofix import agent_loop, llm_backend

    original_run_prompt = llm_backend.run_prompt

    def wrapped_run_prompt(prompt: str, **kwargs: Any):
        started = time.monotonic()
        result = original_run_prompt(prompt, **kwargs)
        usage = {
            "prompt_tokens": _estimate_tokens(prompt),
            "completion_tokens": _estimate_tokens(result.stdout + ("\n" + result.stderr if result.stderr else "")),
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        _write_event(
            trace_file,
            {
                "name": "llm_call",
                "event_type": "llm",
                "ok": bool(result.returncode == 0),
                "duration_seconds": round(time.monotonic() - started, 6),
                "model": str(kwargs.get("model") or ""),
                "returncode": int(result.returncode),
                "usage": usage,
            },
        )
        return result

    llm_backend.run_prompt = wrapped_run_prompt
    agent_loop.run_prompt = wrapped_run_prompt

    original_execute_action = agent_loop._execute_action

    def wrapped_execute_action(action: dict, **kwargs: Any):
        started = time.monotonic()
        kind = str(action.get("action", "unknown") or "unknown")
        try:
            result = original_execute_action(action, **kwargs)
        except Exception as exc:
            _write_event(
                trace_file,
                {
                    "name": f"tool::{kind}",
                    "event_type": "tool",
                    "tool_name": kind,
                    "ok": False,
                    "duration_seconds": round(time.monotonic() - started, 6),
                    "error": str(exc),
                },
            )
            raise

        _write_event(
            trace_file,
            {
                "name": f"tool::{kind}",
                "event_type": "tool",
                "tool_name": kind,
                "ok": True,
                "duration_seconds": round(time.monotonic() - started, 6),
                "result_summary": str(result)[:200],
            },
        )
        return result

    agent_loop._execute_action = wrapped_execute_action


def _write_usage_summary(trace_file: Path, usage_file: Path) -> None:
    prompt_tokens = 0
    completion_tokens = 0
    for event in read_trace_events(trace_file):
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens += int(usage.get("completion_tokens", 0) or 0)
    summary = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "exact": False,
    }
    usage_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _run_task(args: argparse.Namespace) -> int:
    task = json.loads(Path(args.task_file).read_text(encoding="utf-8"))
    workdir = Path(args.workdir).resolve()
    trace_file = Path(args.trace_file).resolve()
    usage_file = Path(args.usage_file).resolve()
    scope = _scope(task)
    _install_trace_patches(trace_file)

    backend_config = LLMBackendConfig(
        backend=args.backend,
        base_url=args.base_url,
        api_key=args.api_key,
    )

    review_prompt_parts = [
        "Audit the current repository for the bug or missing behavior described below.",
        f"Task: {task.get('title', args.task_id)}",
        f"Focus: {task.get('instruction', '')}",
        "Only report actionable issues in source files.",
        "Return via finish_review when done.",
    ]
    scope_text = _scope_block(scope)
    if scope_text:
        review_prompt_parts.append(scope_text)
    verification_text = _verification_block(task)
    if verification_text:
        review_prompt_parts.append(verification_text)

    review_result = run_review_agent_loop(
        root=workdir,
        task_prompt="\n\n".join(part for part in review_prompt_parts if part),
        model=args.model,
        backend_config=backend_config,
        max_steps=max(4, args.max_steps // 2),
        subprocess_module=subprocess,
        timeout=args.timeout,
    )

    findings = _filter_findings(_parse_findings(review_result.findings_json), scope["allowed_files"])
    if not findings:
        findings = [
            {
                "description": str(task.get("instruction", "")),
                "file": _fallback_file_hint(task, scope),
            }
        ]

    any_fix_ok = False
    for finding in findings[:3]:
        prompt_parts = [
            "Fix the following issue in the current worktree.",
            f"Task: {task.get('title', args.task_id)}",
            f"Issue: {finding.get('description', task.get('instruction', ''))}",
        ]
        file_hint = str(finding.get("file", "") or "")
        if file_hint:
            prompt_parts.append(f"File hint: {file_hint}")
        if scope_text:
            prompt_parts.append(scope_text)
        if verification_text:
            prompt_parts.append(verification_text)
        prompt_parts.append("Keep the patch minimal and return finish when done.")

        fix_result = run_agent_loop(
            root=workdir,
            task_prompt="\n\n".join(prompt_parts),
            model=args.model,
            backend_config=backend_config,
            max_steps=args.max_steps,
            subprocess_module=subprocess,
            timeout=args.timeout,
        )
        any_fix_ok = any_fix_ok or fix_result.ok

    _write_usage_summary(trace_file, usage_file)
    return 0 if any_fix_ok or review_result.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--usage-file", required=True)
    parser.add_argument("--trace-file", required=True)
    parser.add_argument("--model", default="default")
    parser.add_argument("--backend", default="claude_cli")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    return _run_task(args)


if __name__ == "__main__":
    raise SystemExit(main())
