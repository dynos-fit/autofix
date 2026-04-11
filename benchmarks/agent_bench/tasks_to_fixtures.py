"""Convert the local task corpus into agent-bench fixture directories."""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _command_to_argv(command: str) -> list[str]:
    formatted = command.format(
        python_executable=sys.executable,
        workdir=".",
    )
    return [str(part) for part in shlex.split(formatted) if str(part).strip()]


def materialize_agent_bench_fixtures(
    tasks_root: Path,
    fixtures_root: Path,
    *,
    only: list[str] | None = None,
) -> list[str]:
    tasks_root = tasks_root.resolve()
    fixtures_root = fixtures_root.resolve()
    fixtures_root.mkdir(parents=True, exist_ok=True)
    selected = set(only or [])
    fixture_ids: list[str] = []

    for task_dir in sorted(tasks_root.iterdir()):
        if not task_dir.is_dir():
            continue
        task_json = task_dir / "task.json"
        repo_dir = task_dir / "repo"
        if not task_json.exists() or not repo_dir.is_dir():
            continue

        task = _load_json(task_json)
        task_id = str(task.get("id") or task_dir.name)
        if selected and task_id not in selected:
            continue

        verification = task.get("verification") or []
        if not isinstance(verification, list) or not verification:
            raise ValueError(f"task {task_id} must declare at least one verification command")
        first_check = verification[0]
        if not isinstance(first_check, dict):
            raise ValueError(f"task {task_id} verification entries must be objects")
        command = str(first_check.get("command") or "").strip()
        if not command:
            raise ValueError(f"task {task_id} verification command must be non-empty")

        fixture_dir = fixtures_root / task_id
        bugged_dir = fixture_dir / "bugged"
        if fixture_dir.exists():
            shutil.rmtree(fixture_dir)
        fixture_dir.mkdir(parents=True)
        shutil.copytree(repo_dir, bugged_dir)

        scope = task.get("scope") if isinstance(task.get("scope"), dict) else {}
        fixture_payload = {
            "id": task_id,
            "name": str(task.get("title") or task_id),
            "category": str(task.get("category") or "unknown"),
            "difficulty": str(task.get("difficulty") or "medium"),
            "description": str(task.get("instruction") or ""),
            "scope": {
                "allowed_files": [str(item) for item in scope.get("allowed_files", [])],
                "forbidden_files": [str(item) for item in scope.get("forbidden_files", [])],
            },
            "test_command": _command_to_argv(command),
        }
        (fixture_dir / "fixture.json").write_text(
            json.dumps(fixture_payload, indent=2),
            encoding="utf-8",
        )
        fixture_ids.append(task_id)

    return fixture_ids
