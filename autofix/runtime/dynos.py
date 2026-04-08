"""Standalone runtime helpers for q-learning, templates, and local telemetry."""

from __future__ import annotations

import random
from pathlib import Path

from autofix.platform import load_json, now_iso, persistent_project_dir, write_json


def project_policy(root: Path) -> dict:
    path = persistent_project_dir(root) / "project-policy.json"
    if not path.exists():
        return {}
    try:
        data = load_json(path)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def log_event(root: Path, event: str, **kwargs) -> None:
    path = persistent_project_dir(root) / "events.jsonl"
    entry = {"event": event, "at": now_iso(), **kwargs}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(__import__("json").dumps(entry) + "\n")


def encode_autofix_state(category: str, file_ext: str, centrality_tier: str, severity: str) -> str:
    return "|".join([category, file_ext, centrality_tier, severity])


def load_autofix_q_table(root: Path) -> dict:
    path = persistent_project_dir(root) / "autofix-qtable.json"
    if not path.exists():
        return {"entries": {}}
    try:
        data = load_json(path)
    except Exception:
        return {"entries": {}}
    return data if isinstance(data, dict) else {"entries": {}}


def save_autofix_q_table(root: Path, table: dict) -> None:
    write_json(persistent_project_dir(root) / "autofix-qtable.json", table)


def select_action(q_table: dict, q_state: str, actions: list[str], *, epsilon: float) -> tuple[str, str]:
    entries = q_table.get("entries", {})
    state_values = entries.get(q_state, {})
    if random.random() < epsilon:
        return random.choice(actions), "explore"
    ranked = sorted(actions, key=lambda action: float(state_values.get(action, 0.0)), reverse=True)
    return ranked[0], "exploit"


def update_q_value(q_table: dict, q_state: str, action: str, reward: float, next_state: object | None) -> None:
    entries = q_table.setdefault("entries", {})
    state_entry = entries.setdefault(q_state, {})
    current = float(state_entry.get(action, 0.0))
    state_entry[action] = round((current * 0.7) + (reward * 0.3), 4)


def find_matching_template(root: Path, finding: dict) -> dict | None:
    path = persistent_project_dir(root) / "fix-templates.json"
    if not path.exists():
        return None
    try:
        data = load_json(path)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    category = finding.get("category")
    evidence_file = str(finding.get("evidence", {}).get("file", "") or "")
    suffix = Path(evidence_file).suffix
    for entry in reversed(data):
        if not isinstance(entry, dict):
            continue
        if entry.get("category") != category:
            continue
        if suffix and entry.get("file_suffix") not in ("", suffix):
            continue
        return entry
    return None


def save_fix_template(root: Path, finding: dict, diff_text: str) -> None:
    path = persistent_project_dir(root) / "fix-templates.json"
    try:
        existing = load_json(path) if path.exists() else []
    except Exception:
        existing = []
    if not isinstance(existing, list):
        existing = []
    evidence_file = str(finding.get("evidence", {}).get("file", "") or "")
    existing.append(
        {
            "saved_at": now_iso(),
            "category": finding.get("category", ""),
            "file_suffix": Path(evidence_file).suffix,
            "diff": diff_text,
        }
    )
    write_json(path, existing[-50:])


def get_neighbor_file_contents(root: Path, evidence_file: str, *, max_files: int, max_lines: int) -> list[dict]:
    target = root / evidence_file
    if not target.exists():
        return []
    parent = target.parent
    neighbors: list[dict] = []
    for path in sorted(parent.glob("*")):
        if path == target or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = content.splitlines()[:max_lines]
        neighbors.append({"path": str(path.relative_to(root)), "content": "\n".join(lines)})
        if len(neighbors) >= max_files:
            break
    return neighbors
