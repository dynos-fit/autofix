"""Portable platform helpers for the standalone autofix package."""

from __future__ import annotations

import ast
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> object:
    return json.loads(path.read_text())


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def new_scan_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


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


def aggregate_state_dir(root: Path) -> Path:
    return runtime_state_dir(root) / "state"


def scans_root(root: Path) -> Path:
    return runtime_state_dir(root) / "scans"


def current_scan_dir(root: Path) -> Path | None:
    scan_id = os.environ.get("AUTOFIX_SCAN_ID", "").strip()
    if not scan_id:
        return None
    return scans_root(root) / scan_id


def write_scan_artifact(root: Path, name: str, data: object) -> Path | None:
    scan_dir = current_scan_dir(root)
    if scan_dir is None:
        return None
    path = scan_dir / name
    write_json(path, data)
    return path


def persistent_project_dir(root: Path) -> Path:
    explicit = os.environ.get("AUTOFIX_PERSISTENT_DIR")
    if explicit:
        path = Path(explicit)
        path.mkdir(parents=True, exist_ok=True)
        return path
    path = runtime_state_dir(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def collect_retrospectives(root: Path) -> list[dict]:
    retrospectives: list[dict] = []
    for path in sorted(runtime_state_dir(root).glob("task-*/task-retrospective.json")):
        try:
            data = load_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            retrospectives.append(data)
    return retrospectives


def build_import_graph(root: Path) -> dict:
    edges: list[dict] = []
    pagerank: dict[str, float] = {}
    py_files = sorted(root.rglob("*.py"))
    module_to_path: dict[str, str] = {}
    for path in py_files:
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            continue
        module_to_path[path.stem] = rel
        pagerank[rel] = 0.0

    for path in py_files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
            rel = str(path.relative_to(root))
        except (OSError, SyntaxError, ValueError):
            continue
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        for imported in imports:
            target = module_to_path.get(imported)
            if not target:
                continue
            edges.append({"from": rel, "to": target})
            pagerank[rel] = pagerank.get(rel, 0.0) + 1.0
            pagerank[target] = pagerank.get(target, 0.0) + 1.0
    return {"edges": edges, "pagerank": pagerank}


def is_generated_file(path: str) -> bool:
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
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(root),
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []

    finding_counts: dict[str, int] = {}
    for finding in findings:
        evidence_file = str(finding.get("evidence", {}).get("file", "") or "")
        if evidence_file:
            finding_counts[evidence_file] = finding_counts.get(evidence_file, 0) + 1

    now = datetime.now(timezone.utc)
    file_coverage = coverage.get("files", {})
    scored: list[tuple[str, float]] = []
    for line in result.stdout.splitlines():
        rel = line.strip()
        if not rel or is_generated_file(rel):
            continue
        full_path = root / rel
        if not full_path.is_file():
            continue
        if full_path.suffix.lower() not in {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java"}:
            continue
        score = float(finding_counts.get(rel, 0) * 3 + len(rel) / 100.0)
        scan_info = file_coverage.get(rel, {})
        last_scanned = str(scan_info.get("last_scanned_at", "") or "")
        if last_scanned:
            try:
                scanned_dt = datetime.fromisoformat(last_scanned.replace("Z", "+00:00"))
                age_hours = (now - scanned_dt).total_seconds() / 3600
                if age_hours < 24:
                    score -= 10
            except ValueError:
                pass
        scored.append((rel, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:max_files]


def local_patterns_path(root: Path) -> Path:
    return persistent_project_dir(root) / "patterns.md"
