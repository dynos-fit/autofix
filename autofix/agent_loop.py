"""Bounded local coding-agent loop for low-token backends."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from autofix.benchmarking import benchmark_trace_tool
from autofix.llm_backend import LLMBackendConfig, run_prompt

PROMPTS_DIR = Path(__file__).resolve().parent / "llm_io" / "prompts"
FIX_SYSTEM_PROMPT_PATH = PROMPTS_DIR / "agent_fix_system.md"
REVIEW_SYSTEM_PROMPT_PATH = PROMPTS_DIR / "agent_review_system.md"


@dataclass(frozen=True)
class AgentRunResult:
    ok: bool
    summary: str = ""
    error: str = ""
    steps: int = 0
    findings_json: str = ""

_BLOCKED_PATH_PARTS = {".git", ".autofix", ".dynos"}


def _load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [line for line in stripped.splitlines() if not line.startswith("```")]
        stripped = "\n".join(lines).strip()
    return stripped


def _parse_action(raw: str) -> dict:
    payload = _strip_fences(raw)
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Model output was not a JSON object")
    action = str(data.get("action", "") or "").strip()
    if not action:
        raise ValueError("Missing action")
    return data


def _resolve_path(root: Path, rel_path: str) -> Path:
    candidate = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError(f"path escapes worktree: {rel_path}")
    if any(part in _BLOCKED_PATH_PARTS for part in candidate.relative_to(root_resolved).parts):
        raise ValueError(f"path is not accessible to the agent: {rel_path}")
    return candidate


def _truncate(text: str, *, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _is_allowed_command(parts: list[str]) -> bool:
    if not parts:
        return False
    allowed_prefixes = (
        ["python3", "-m", "pytest"],
        ["python", "-m", "pytest"],
        ["pytest"],
        ["git", "diff"],
        ["git", "status"],
        ["git", "log"],
    )
    return any(parts[: len(prefix)] == prefix for prefix in allowed_prefixes)


@benchmark_trace_tool
def execute_action(action: dict, *, root: Path, subprocess_module) -> str:
    kind = str(action.get("action", "") or "")
    if kind == "list_files":
        rel = str(action.get("path", ".") or ".")
        target = _resolve_path(root, rel)
        files = []
        for path in sorted(target.rglob("*")):
            if any(part in _BLOCKED_PATH_PARTS for part in path.relative_to(root).parts):
                continue
            if path.is_file():
                files.append(str(path.relative_to(root)))
            if len(files) >= 200:
                break
        return json.dumps({"files": files}, indent=2)

    if kind == "read_file":
        rel = str(action.get("path", "") or "")
        if not rel:
            raise ValueError("read_file requires path")
        start_line = max(int(action.get("start_line", 1) or 1), 1)
        end_line = max(int(action.get("end_line", start_line + 199) or (start_line + 199)), start_line)
        path = _resolve_path(root, rel)
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = content[start_line - 1 : end_line]
        numbered = "\n".join(f"{start_line + idx}: {line}" for idx, line in enumerate(selected))
        return json.dumps({"path": rel, "start_line": start_line, "end_line": end_line, "content": numbered})

    if kind == "search":
        pattern = str(action.get("pattern", "") or "")
        if not pattern:
            raise ValueError("search requires pattern")
        rel = str(action.get("path", ".") or ".")
        target = _resolve_path(root, rel)
        result = subprocess_module.run(
            [
                "rg",
                "-n",
                "--hidden",
                "--glob",
                "!.git",
                "--glob",
                "!.autofix",
                "--glob",
                "!.dynos",
                pattern,
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(root),
        )
        return json.dumps({"matches": _truncate(result.stdout, limit=5000), "returncode": result.returncode})

    if kind == "write_file":
        rel = str(action.get("path", "") or "")
        content = action.get("content")
        if not rel or not isinstance(content, str):
            raise ValueError("write_file requires path and string content")
        path = _resolve_path(root, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return json.dumps({"ok": True, "path": rel, "bytes": len(content.encode('utf-8'))})

    if kind == "replace_text":
        rel = str(action.get("path", "") or "")
        old = action.get("old")
        new = action.get("new")
        count = int(action.get("count", 1) or 1)
        if not rel or not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("replace_text requires path, old, new")
        path = _resolve_path(root, rel)
        content = path.read_text(encoding="utf-8", errors="replace")
        replacements = content.count(old)
        if replacements == 0:
            raise ValueError("replace_text old text not found")
        updated = content.replace(old, new, count)
        path.write_text(updated, encoding="utf-8")
        return json.dumps({"ok": True, "path": rel, "replacements_available": replacements, "count": count})

    if kind == "run_command":
        command = str(action.get("command", "") or "")
        parts = shlex.split(command)
        if not _is_allowed_command(parts):
            raise ValueError(f"command not allowed: {command}")
        result = subprocess_module.run(
            parts,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(root),
        )
        return json.dumps(
            {
                "returncode": result.returncode,
                "stdout": _truncate(result.stdout, limit=5000),
                "stderr": _truncate(result.stderr, limit=3000),
            }
        )

    if kind == "git_diff":
        result = subprocess_module.run(
            ["git", "diff", "--stat"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(root),
        )
        return json.dumps({"returncode": result.returncode, "stdout": _truncate(result.stdout, limit=4000)})

    if kind == "finish":
        return "__finish__"

    if kind == "finish_review":
        return "__finish_review__"

    raise ValueError(f"unknown action: {kind}")


_execute_action = execute_action


def run_agent_loop(
    *,
    root: Path,
    task_prompt: str,
    model: str | None,
    backend_config: LLMBackendConfig,
    max_steps: int,
    subprocess_module,
    timeout: int,
) -> AgentRunResult:
    system_prompt = _load_prompt(FIX_SYSTEM_PROMPT_PATH)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
    ]
    for step in range(1, max_steps + 1):
        prompt = (
            system_prompt
            + "\n\nConversation so far:\n"
            + "\n\n".join(f"{item['role'].upper()}:\n{item['content']}" for item in messages[1:])
        )
        result = run_prompt(
            prompt,
            model=model,
            config=backend_config,
            timeout=timeout,
            cwd=root,
            subprocess_module=subprocess_module,
        )
        if result.returncode != 0:
            return AgentRunResult(ok=False, error=result.stderr or "agent model call failed", steps=step)
        try:
            action = _parse_action(result.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            return AgentRunResult(ok=False, error=f"invalid agent output: {exc}", steps=step)

        messages.append({"role": "assistant", "content": result.stdout})
        if str(action.get("action")) == "finish":
            return AgentRunResult(ok=True, summary=str(action.get("summary", "") or ""), steps=step)

        try:
            tool_result = execute_action(action, root=root, subprocess_module=subprocess_module)
        except Exception as exc:
            tool_result = json.dumps({"error": str(exc)})
        messages.append({"role": "user", "content": f"Tool result:\n{tool_result}"})
    return AgentRunResult(ok=False, error="agent exceeded max steps", steps=max_steps)


def run_review_agent_loop(
    *,
    root: Path,
    task_prompt: str,
    model: str | None,
    backend_config: LLMBackendConfig,
    max_steps: int,
    subprocess_module,
    timeout: int,
) -> AgentRunResult:
    system_prompt = _load_prompt(REVIEW_SYSTEM_PROMPT_PATH)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
    ]
    for step in range(1, max_steps + 1):
        prompt = (
            system_prompt
            + "\n\nConversation so far:\n"
            + "\n\n".join(f"{item['role'].upper()}:\n{item['content']}" for item in messages[1:])
        )
        result = run_prompt(
            prompt,
            model=model,
            config=backend_config,
            timeout=timeout,
            cwd=root,
            subprocess_module=subprocess_module,
        )
        if result.returncode != 0:
            return AgentRunResult(ok=False, error=result.stderr or "review agent model call failed", steps=step)
        try:
            action = _parse_action(result.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            return AgentRunResult(ok=False, error=f"invalid review agent output: {exc}", steps=step)

        messages.append({"role": "assistant", "content": result.stdout})
        if str(action.get("action")) == "finish_review":
            findings = action.get("findings", [])
            if not isinstance(findings, list):
                return AgentRunResult(ok=False, error="finish_review findings must be a list", steps=step)
            return AgentRunResult(ok=True, summary="review completed", findings_json=json.dumps(findings), steps=step)

        try:
            tool_result = execute_action(action, root=root, subprocess_module=subprocess_module)
        except Exception as exc:
            tool_result = json.dumps({"error": str(exc)})
        messages.append({"role": "user", "content": f"Tool result:\n{tool_result}"})
    return AgentRunResult(ok=False, error="review agent exceeded max steps", steps=max_steps)
