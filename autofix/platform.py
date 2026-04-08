"""Portable platform helpers for the extracted autofix package."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> object:
    return json.loads(path.read_text())


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def runtime_state_dir(root: Path) -> Path:
    explicit = os.environ.get("AUTOFIX_RUNTIME_DIR")
    if explicit:
        return Path(explicit)
    autofix_dir = root / ".autofix"
    dynos_dir = root / ".dynos"
    if autofix_dir.exists():
        return autofix_dir
    if dynos_dir.exists():
        return dynos_dir
    return autofix_dir


def persistent_project_dir(root: Path) -> Path:
    explicit = os.environ.get("AUTOFIX_PERSISTENT_DIR")
    if explicit:
        path = Path(explicit)
        path.mkdir(parents=True, exist_ok=True)
        return path
    try:
        from dynoslib_core import _persistent_project_dir  # type: ignore

        return _persistent_project_dir(root)
    except ImportError:
        path = runtime_state_dir(root)
        path.mkdir(parents=True, exist_ok=True)
        return path


def collect_retrospectives(root: Path) -> list[dict]:
    try:
        from dynoslib_core import collect_retrospectives as _collect_retrospectives  # type: ignore

        result = _collect_retrospectives(root)
        return result if isinstance(result, list) else []
    except ImportError:
        return []


def build_import_graph(root: Path) -> dict:
    try:
        from dynoslib_crawler import build_import_graph as _build_import_graph  # type: ignore

        result = _build_import_graph(root)
        return result if isinstance(result, dict) else {"pagerank": {}}
    except ImportError:
        return {"pagerank": {}}


def is_generated_file(path: str) -> bool:
    try:
        from dynoslib_crawler import _is_generated_file as _impl  # type: ignore

        return bool(_impl(path))
    except ImportError:
        generated_markers = (
            ".generated.",
            ".g.",
            ".pb.",
            "node_modules/",
            "dist/",
            "build/",
        )
        return any(marker in path for marker in generated_markers)


def compute_scan_targets(
    root: Path,
    *,
    max_files: int,
    coverage: dict,
    findings: list[dict],
) -> list[tuple[str, float]]:
    try:
        from dynoslib_crawler import compute_scan_targets as _compute_scan_targets  # type: ignore

        result = _compute_scan_targets(root, max_files=max_files, coverage=coverage, findings=findings)
        return result if isinstance(result, list) else []
    except ImportError:
        return []


def local_patterns_path(root: Path) -> Path:
    try:
        from dynopatterns import local_patterns_path as _local_patterns_path  # type: ignore

        return _local_patterns_path(root)
    except ImportError:
        return persistent_project_dir(root) / "patterns.md"
