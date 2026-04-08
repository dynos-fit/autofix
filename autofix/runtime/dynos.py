"""Optional Dynos compatibility surface used by the standalone app."""

from __future__ import annotations

from pathlib import Path


def project_policy(root: Path) -> dict:
    try:
        from dynoslib_core import project_policy as _project_policy  # type: ignore

        result = _project_policy(root)
        return result if isinstance(result, dict) else {}
    except ImportError:
        return {}


def log_event(root: Path, event: str, **kwargs) -> None:
    try:
        from dynoslib_log import log_event as _log_event  # type: ignore

        _log_event(root, event, **kwargs)
    except ImportError:
        return


def encode_autofix_state(category: str, file_ext: str, centrality_tier: str, severity: str) -> str:
    try:
        from dynoslib_qlearn import encode_autofix_state as _impl  # type: ignore

        return _impl(category, file_ext, centrality_tier, severity)
    except ImportError:
        return "|".join([category, file_ext, centrality_tier, severity])


def load_autofix_q_table(root: Path) -> dict:
    try:
        from dynoslib_qlearn import load_autofix_q_table as _impl  # type: ignore

        result = _impl(root)
        return result if isinstance(result, dict) else {"entries": {}}
    except ImportError:
        return {"entries": {}}


def save_autofix_q_table(root: Path, table: dict) -> None:
    try:
        from dynoslib_qlearn import save_autofix_q_table as _impl  # type: ignore

        _impl(root, table)
    except ImportError:
        return


def select_action(q_table: dict, q_state: str, actions: list[str], *, epsilon: float) -> tuple[str, str]:
    try:
        from dynoslib_qlearn import select_action as _impl  # type: ignore

        return _impl(q_table, q_state, actions, epsilon=epsilon)
    except ImportError:
        return actions[0], "fallback"


def update_q_value(q_table: dict, q_state: str, action: str, reward: float, next_state: object | None) -> None:
    try:
        from dynoslib_qlearn import update_q_value as _impl  # type: ignore

        _impl(q_table, q_state, action, reward, next_state)
    except ImportError:
        entries = q_table.setdefault("entries", {})
        state_entry = entries.setdefault(q_state, {})
        state_entry[action] = reward


def find_matching_template(root: Path, finding: dict) -> dict | None:
    try:
        from dynoslib_templates import find_matching_template as _impl  # type: ignore

        result = _impl(root, finding)
        return result if isinstance(result, dict) else None
    except ImportError:
        return None


def save_fix_template(root: Path, finding: dict, diff_text: str) -> None:
    try:
        from dynoslib_templates import save_fix_template as _impl  # type: ignore

        _impl(root, finding, diff_text)
    except ImportError:
        return


def get_neighbor_file_contents(root: Path, evidence_file: str, *, max_files: int, max_lines: int) -> list[dict]:
    try:
        from dynoslib_crawler import get_neighbor_file_contents as _impl  # type: ignore

        result = _impl(root, evidence_file, max_files=max_files, max_lines=max_lines)
        return result if isinstance(result, list) else []
    except ImportError:
        return []
