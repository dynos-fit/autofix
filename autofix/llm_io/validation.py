"""Strict parsing, validation, and repair for LLM review output."""

from __future__ import annotations

import json
from pathlib import Path

from autofix.defaults import LLM_INVOCATION_TIMEOUT

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
FORMAT_REPAIR_PROMPT_PATH = PROMPTS_DIR / "llm_format_repair.md"
FORMAT_REGENERATE_PROMPT_PATH = PROMPTS_DIR / "llm_regenerate.md"


def extract_json_array(raw_output: str) -> list | None:
    output = raw_output.strip()
    if output.startswith("```"):
        output = "\n".join(line for line in output.splitlines() if not line.startswith("```")).strip()
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        start = output.find("[")
        end = output.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(output[start:end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, list) else None


def validate_llm_issue(issue: object, *, allowed_files: set[str]) -> dict | None:
    if not isinstance(issue, dict):
        return None
    required = {"description", "file", "line", "severity", "category_detail", "confidence"}
    if not required.issubset(issue.keys()):
        return None

    description = str(issue.get("description", "") or "").strip()
    file_name = str(issue.get("file", "") or "").strip()
    category_detail = str(issue.get("category_detail", "") or "").strip()
    severity = str(issue.get("severity", "") or "").strip().lower()
    if not description or not file_name or not category_detail:
        return None
    if file_name not in allowed_files:
        return None
    if severity not in {"low", "medium", "high", "critical"}:
        return None

    try:
        line_num = int(issue.get("line"))
    except (TypeError, ValueError):
        return None
    if line_num <= 0:
        return None

    try:
        confidence = float(issue.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None

    return {
        "description": description,
        "file": file_name,
        "line": line_num,
        "severity": severity,
        "category_detail": category_detail,
        "confidence": confidence,
    }


def validate_llm_issues(issues: list, *, allowed_files: set[str]) -> list[dict]:
    validated: list[dict] = []
    for issue in issues:
        item = validate_llm_issue(issue, allowed_files=allowed_files)
        if item is not None:
            validated.append(item)
    return validated


def repair_llm_output(
    raw_output: str,
    *,
    allowed_files: list[str],
    subprocess_module,
    cwd: Path,
) -> str | None:
    prompt = FORMAT_REPAIR_PROMPT_PATH.read_text(encoding="utf-8")
    prompt = prompt.replace("{{allowed_files}}", json.dumps(allowed_files, indent=2))
    prompt = prompt.replace("{{raw_output}}", raw_output)
    try:
        result = subprocess_module.run(
            ["claude", "-p", prompt, "--model", "haiku"],
            capture_output=True,
            text=True,
            timeout=LLM_INVOCATION_TIMEOUT,
            cwd=str(cwd),
        )
    except (subprocess_module.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def regenerate_llm_output(
    *,
    review_prompt: str,
    bad_output: str,
    allowed_files: list[str],
    subprocess_module,
    cwd: Path,
) -> str | None:
    prompt = FORMAT_REGENERATE_PROMPT_PATH.read_text(encoding="utf-8")
    prompt = prompt.replace("{{allowed_files}}", json.dumps(allowed_files, indent=2))
    prompt = prompt.replace("{{review_prompt}}", review_prompt)
    prompt = prompt.replace("{{bad_output}}", bad_output)
    try:
        result = subprocess_module.run(
            ["claude", "-p", prompt, "--model", "haiku"],
            capture_output=True,
            text=True,
            timeout=LLM_INVOCATION_TIMEOUT,
            cwd=str(cwd),
        )
    except (subprocess_module.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout
