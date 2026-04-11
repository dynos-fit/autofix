#!/usr/bin/env python3
"""Deterministic smoke-test agent for the benchmark harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected text not found in {path}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _append_trace(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-file")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--usage-file", required=True)
    parser.add_argument("--trace-file")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    task_id = args.task_id

    if task_id == "bugfix_take_limit":
        _replace(workdir / "src" / "calc.py", "return items[: limit - 1]", "return items[:limit]")
    elif task_id == "feature_todo_done_filter":
        path = workdir / "src" / "todo_cli.py"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "    return [task[\"title\"] for task in tasks]\n",
                "    if done_only:\n"
                "        tasks = [task for task in tasks if task.get(\"done\")]\n"
                "    return [task[\"title\"] for task in tasks]\n",
                1,
            ).replace(
                "def list_titles(tasks):\n",
                "def list_titles(tasks, done_only=False):\n",
                1,
            ),
            encoding="utf-8",
        )
    elif task_id == "bugfix_port_parser":
        _replace(
            workdir / "src" / "config.py",
            "        return 0\n",
            "        raise ValueError(f\"invalid port: {value}\")\n",
        )
    else:
        raise SystemExit(f"unknown task id: {task_id}")

    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 80,
        "total_tokens": 200,
        "exact": True,
    }
    Path(args.usage_file).write_text(json.dumps(usage, indent=2), encoding="utf-8")
    if args.trace_file:
        trace_path = Path(args.trace_file)
        _append_trace(
            trace_path,
            {
                "name": "llm_call",
                "event_type": "llm",
                "ok": True,
                "duration_seconds": 0.01,
                "usage": usage,
            },
        )
        _append_trace(
            trace_path,
            {
                "name": "tool::read_file",
                "event_type": "tool",
                "tool_name": "read_file",
                "ok": True,
                "duration_seconds": 0.002,
            },
        )
        _append_trace(
            trace_path,
            {
                "name": "tool::write_file",
                "event_type": "tool",
                "tool_name": "write_file",
                "ok": True,
                "duration_seconds": 0.003,
            },
        )
    print(f"mock agent solved {task_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
