"""Shared LLM backend boundary for CLI- and HTTP-based providers."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LLMBackendConfig:
    backend: str = "claude_cli"
    base_url: str = ""
    api_key: str = ""


@dataclass(frozen=True)
class LLMResult:
    returncode: int
    stdout: str
    stderr: str = ""


def build_claude_prompt_command(prompt: str, *, model: str | None = None) -> list[str]:
    command = ["claude", "-p", prompt]
    model_name = (model or "").strip()
    if model_name and model_name.lower() != "default":
        command.extend(["--model", model_name])
    return command


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _extract_message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices", [])
    if not isinstance(choices, list) or not choices:
        raise ValueError("No choices in chat completion response")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("Invalid choice payload")
    message = first.get("message", {})
    if not isinstance(message, dict):
        raise ValueError("Invalid message payload")
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_value = item.get("text", "")
                if isinstance(text_value, str):
                    texts.append(text_value)
        return "\n".join(texts)
    raise ValueError("Unsupported message content format")


def run_prompt(
    prompt: str,
    *,
    model: str | None,
    config: LLMBackendConfig,
    timeout: int,
    cwd: Path,
    subprocess_module=subprocess,
) -> LLMResult:
    if config.backend == "claude_cli":
        command = build_claude_prompt_command(prompt, model=model)
        try:
            result = subprocess_module.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd),
            )
        except subprocess_module.TimeoutExpired:
            raise
        except OSError as exc:
            return LLMResult(returncode=1, stdout="", stderr=str(exc))
        return LLMResult(
            returncode=int(result.returncode),
            stdout=str(result.stdout),
            stderr=str(result.stderr),
        )

    if config.backend != "openai_compatible":
        return LLMResult(returncode=1, stdout="", stderr=f"Unsupported llm backend: {config.backend}")

    if not config.base_url.strip():
        return LLMResult(returncode=1, stdout="", stderr="llm_base_url is required for openai_compatible backend")

    body = {
        "model": (model or "").strip() or "default",
        "messages": [{"role": "user", "content": prompt}],
    }
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        _chat_completions_url(config.base_url),
        data=data,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return LLMResult(returncode=1, stdout="", stderr=f"HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        return LLMResult(returncode=1, stdout="", stderr=str(exc))
    except TimeoutError:
        raise subprocess.TimeoutExpired(cmd="openai_compatible", timeout=timeout)

    try:
        payload = json.loads(raw)
        content = _extract_message_content(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        return LLMResult(returncode=1, stdout="", stderr=f"Invalid chat completion response: {exc}")
    return LLMResult(returncode=0, stdout=content, stderr="")
