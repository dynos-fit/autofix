"""Thin agent-bench adapter for the real autofix review and fix loops."""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from autofix.agent_loop import run_agent_loop, run_review_agent_loop
from autofix.llm_backend import LLMBackendConfig

if TYPE_CHECKING:
    from agent_bench.fixture import Fixture


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


def _scope_block(allowed_files: list[str], forbidden_files: list[str]) -> str:
    lines: list[str] = []
    if allowed_files:
        lines.append("Only modify: " + ", ".join(allowed_files))
    if forbidden_files:
        lines.append("Do not modify: " + ", ".join(forbidden_files))
    return "\n".join(lines)


def _verification_block(test_command: list[str]) -> str:
    if not test_command:
        return ""
    command = " ".join(shlex.quote(part) for part in test_command)
    return "Verification command:\n- " + command


def _fallback_file_hint(description: str, allowed_files: list[str]) -> str:
    if allowed_files:
        return allowed_files[0]
    for token in description.replace("`", " ").split():
        if "/" in token and "." in token:
            return token.strip(".,:;")
    return ""


@dataclass(frozen=True)
class AutofixBenchmarkConfig:
    backend: str = "claude_cli"
    base_url: str = ""
    api_key: str = ""
    model: str | None = None
    max_steps: int = 12
    timeout: int = 300
    max_fix_attempts: int = 3


def build_agent(config: AutofixBenchmarkConfig) -> Callable[[Path, "Fixture"], None]:
    def agent(workdir: Path, fixture: "Fixture") -> None:
        backend_config = LLMBackendConfig(
            backend=config.backend,
            base_url=config.base_url,
            api_key=config.api_key,
        )

        scope_text = _scope_block(fixture.allowed_files, fixture.forbidden_files)
        verification_text = _verification_block(fixture.test_command)

        review_prompt_parts = [
            "Audit the repository for the bug or missing behavior described below.",
            f"Task: {fixture.name}",
            f"Issue: {fixture.description}",
            "Only report actionable issues in source files.",
            "Return via finish_review when done.",
        ]
        if scope_text:
            review_prompt_parts.append(scope_text)
        if verification_text:
            review_prompt_parts.append(verification_text)

        review_result = run_review_agent_loop(
            root=workdir,
            task_prompt="\n\n".join(review_prompt_parts),
            model=config.model,
            backend_config=backend_config,
            max_steps=max(4, config.max_steps // 2),
            subprocess_module=subprocess,
            timeout=config.timeout,
        )
        if not review_result.ok:
            raise RuntimeError(review_result.error or "review agent failed")

        findings = _filter_findings(
            _parse_findings(review_result.findings_json),
            fixture.allowed_files,
        )
        if not findings:
            findings = [
                {
                    "description": fixture.description,
                    "file": _fallback_file_hint(fixture.description, fixture.allowed_files),
                }
            ]

        for finding in findings[: max(1, config.max_fix_attempts)]:
            prompt_parts = [
                "Fix the following issue in the current worktree.",
                f"Task: {fixture.name}",
                f"Issue: {finding.get('description', fixture.description)}",
            ]
            file_hint = str(finding.get("file") or "")
            if file_hint:
                prompt_parts.append(f"File hint: {file_hint}")
            if scope_text:
                prompt_parts.append(scope_text)
            if verification_text:
                prompt_parts.append(verification_text)
            prompt_parts.append("Keep the patch minimal and return finish when done.")

            run_agent_loop(
                root=workdir,
                task_prompt="\n\n".join(prompt_parts),
                model=config.model,
                backend_config=backend_config,
                max_steps=config.max_steps,
                subprocess_module=subprocess,
                timeout=config.timeout,
            )

    return agent
