"""Prompt loading and construction for LLM review."""

from __future__ import annotations

from pathlib import Path

from autofix.defaults import (
    LLM_REVIEW_CHUNK_LINES,
    LLM_REVIEW_CHUNK_MAX_PER_FILE,
    LLM_REVIEW_CHUNK_OVERLAP,
    LLM_REVIEW_CHUNK_PROMPT_KEY,
    LLM_REVIEW_CHUNK_THRESHOLD,
    LLM_REVIEW_FILE_TRUNCATION,
    LLM_REVIEW_PROMPT_KEY,
)
from autofix.platform import persistent_project_dir

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
REVIEW_PROMPT_PATH = PROMPTS_DIR / "haiku_review.md"
CHUNK_REVIEW_PROMPT_PATH = PROMPTS_DIR / "haiku_review_chunk.md"
PROMPT_PATHS = {
    "full": REVIEW_PROMPT_PATH,
    "chunk": CHUNK_REVIEW_PROMPT_PATH,
}


def resolve_prompt_path(prompt_key: str) -> Path:
    try:
        return PROMPT_PATHS[prompt_key]
    except KeyError as exc:
        raise ValueError(f"Unknown prompt key: {prompt_key}") from exc


def load_prompt_template(*, prompt_key: str = LLM_REVIEW_PROMPT_KEY, path: Path | None = None) -> str:
    template_path = path or resolve_prompt_path(prompt_key)
    return template_path.read_text(encoding="utf-8")


def build_review_prompt(
    root: Path,
    *,
    selected_files: list[dict],
    review_files: list[str],
    findings_list: list[dict],
    file_truncation: int = LLM_REVIEW_FILE_TRUNCATION,
    prompt_key: str = LLM_REVIEW_PROMPT_KEY,
) -> str:
    project_patterns = ""

    try:
        patterns_path = persistent_project_dir(root) / "dynos_patterns.md"
        if patterns_path.exists():
            prevention_text = ""
            in_prevention = False
            for line in patterns_path.read_text().splitlines():
                if "## Prevention Rules" in line:
                    in_prevention = True
                    continue
                if in_prevention and line.startswith("##"):
                    break
                if in_prevention:
                    prevention_text += line + "\n"
            prevention_text = prevention_text.strip()
            if prevention_text:
                project_patterns = f"## Project-Specific Patterns To Watch For\n\n{prevention_text}\n"
    except OSError:
        pass

    file_sections: list[str] = []
    for rel in review_files:
        path = root / rel
        try:
            content = path.read_text()
        except OSError:
            continue
        lines = content.splitlines()
        if len(lines) > file_truncation:
            content = "\n".join(lines[:file_truncation]) + f"\n... (truncated, {len(lines)} total lines)"
        file_sections.append(f"--- {rel} ---\n{content}\n")

    prompt = load_prompt_template(prompt_key=prompt_key)
    prompt = prompt.replace("{{project_patterns}}", project_patterns.strip())
    prompt = prompt.replace("{{file_sections}}", "\n\n".join(section.strip() for section in file_sections if section.strip()))
    return prompt.strip() + "\n"


def build_review_prompt_for_file(
    root: Path,
    *,
    selected_files: list[dict],
    review_file: str,
    findings_list: list[dict],
    file_truncation: int = LLM_REVIEW_FILE_TRUNCATION,
    prompt_key: str = LLM_REVIEW_PROMPT_KEY,
) -> str:
    return build_review_prompt(
        root,
        selected_files=selected_files,
        review_files=[review_file],
        findings_list=findings_list,
        file_truncation=file_truncation,
        prompt_key=prompt_key,
    )


def build_review_chunks_for_file(
    root: Path,
    *,
    review_file: str,
    reviewed_chunk_keys: set[str] | None = None,
    chunk_threshold: int = LLM_REVIEW_CHUNK_THRESHOLD,
    chunk_lines: int = LLM_REVIEW_CHUNK_LINES,
    chunk_max_per_file: int = LLM_REVIEW_CHUNK_MAX_PER_FILE,
    chunk_overlap: int = LLM_REVIEW_CHUNK_OVERLAP,
) -> list[dict]:
    path = root / review_file
    try:
        content = path.read_text()
    except OSError:
        return []
    lines = content.splitlines()
    if len(lines) <= chunk_threshold:
        return [{"review_file": review_file, "start_line": 1, "end_line": len(lines), "content": content}]

    chunks: list[dict] = []
    start = 0
    step = max(chunk_lines - chunk_overlap, 1)
    while start < len(lines):
        end = min(start + chunk_lines, len(lines))
        chunk_content = "\n".join(lines[start:end])
        chunks.append(
            {
                "review_file": review_file,
                "start_line": start + 1,
                "end_line": end,
                "content": chunk_content,
                "chunk_key": f"{start + 1}-{end}",
            }
        )
        if end >= len(lines):
            break
        start += step
    if len(chunks) <= chunk_max_per_file:
        return chunks

    reviewed_chunk_keys = reviewed_chunk_keys or set()

    def sample_indexes(pool_size: int, limit: int) -> list[int]:
        if pool_size <= limit:
            return list(range(pool_size))
        selected_indexes: list[int] = []
        last_index = pool_size - 1
        for slot in range(limit):
            if limit == 1:
                index = 0
            else:
                index = round(slot * last_index / (limit - 1))
            if not selected_indexes or index != selected_indexes[-1]:
                selected_indexes.append(index)
        return sorted(set(selected_indexes))

    unseen_chunks = [chunk for chunk in chunks if chunk["chunk_key"] not in reviewed_chunk_keys]
    sampled_chunks: list[dict]
    if len(unseen_chunks) >= chunk_max_per_file:
        sampled_chunks = [unseen_chunks[index] for index in sample_indexes(len(unseen_chunks), chunk_max_per_file)]
    else:
        sampled_chunks = list(unseen_chunks)
        if len(sampled_chunks) < chunk_max_per_file:
            seen_chunks = [chunk for chunk in chunks if chunk["chunk_key"] in reviewed_chunk_keys]
            remaining = chunk_max_per_file - len(sampled_chunks)
            sampled_chunks.extend(seen_chunks[index] for index in sample_indexes(len(seen_chunks), remaining))

    total_chunks = len(chunks)
    for chunk in sampled_chunks:
        chunk["total_chunks"] = total_chunks
        chunk["sampled"] = True
        chunk["previously_reviewed"] = chunk["chunk_key"] in reviewed_chunk_keys
    return sampled_chunks


def build_review_prompt_for_chunk(
    root: Path,
    *,
    review_file: str,
    chunk: dict,
    prompt_key: str = LLM_REVIEW_CHUNK_PROMPT_KEY,
) -> str:
    project_patterns = ""
    try:
        patterns_path = persistent_project_dir(root) / "dynos_patterns.md"
        if patterns_path.exists():
            prevention_text = ""
            in_prevention = False
            for line in patterns_path.read_text().splitlines():
                if "## Prevention Rules" in line:
                    in_prevention = True
                    continue
                if in_prevention and line.startswith("##"):
                    break
                if in_prevention:
                    prevention_text += line + "\n"
            prevention_text = prevention_text.strip()
            if prevention_text:
                project_patterns = f"## Project-Specific Patterns To Watch For\n\n{prevention_text}\n"
    except OSError:
        pass

    prompt = load_prompt_template(prompt_key=prompt_key)
    prompt = prompt.replace("{{project_patterns}}", project_patterns.strip())
    section = (
        f"--- {review_file} (lines {chunk['start_line']}-{chunk['end_line']}) ---\n"
        f"{chunk['content']}\n"
    )
    prompt = prompt.replace("{{file_sections}}", section.strip())
    return prompt.strip() + "\n"
