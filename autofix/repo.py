"""Manage registered repositories in ~/.autofix/repos.json.

Provides repo_add, repo_remove, repo_list for criterion 14 and 15.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RepoResult:
    """Uniform result returned by every repo operation."""

    exit_code: int = 0
    message: str = ""
    output: str = ""


def _repos_file(home_dir: Path) -> Path:
    """Return the path to repos.json, creating parent dirs if needed."""
    autofix_dir = home_dir / ".autofix"
    autofix_dir.mkdir(parents=True, exist_ok=True)
    return autofix_dir / "repos.json"


def _load_repos(repos_path: Path) -> list[dict[str, str]]:
    """Load the repos list from disk, returning [] when the file is absent or corrupt."""
    if not repos_path.exists():
        return []
    try:
        data = json.loads(repos_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _save_repos(repos_path: Path, repos: list[dict[str, str]]) -> None:
    """Persist the repos list to disk."""
    repos_path.write_text(json.dumps(repos, indent=2) + "\n", encoding="utf-8")


def repo_add(path: Path, home_dir: Path) -> RepoResult:
    """Register a git repository.

    Validates that *path* is an existing directory containing a ``.git/``
    subdirectory, resolves it to an absolute path, and appends it to
    ``~/.autofix/repos.json`` (with deduplication).
    """
    resolved = Path(path).resolve()

    # --- validation (criterion 15) ---
    if not resolved.exists():
        return RepoResult(
            exit_code=1,
            message=f"Path does not exist: {resolved}",
        )

    if not resolved.is_dir():
        return RepoResult(
            exit_code=1,
            message=f"Path is not a directory: {resolved}",
        )

    if not (resolved / ".git").is_dir():
        return RepoResult(
            exit_code=1,
            message=f"Directory does not contain a .git/ directory: {resolved}",
        )

    # --- persist ---
    repos_path = _repos_file(home_dir)
    repos = _load_repos(repos_path)

    abs_str = str(resolved)
    existing_paths = {entry["path"] for entry in repos}
    if abs_str not in existing_paths:
        repos.append({"path": abs_str})
        _save_repos(repos_path, repos)

    return RepoResult(
        exit_code=0,
        message=f"Registered repository: {abs_str}",
    )


def repo_remove(path: Path, home_dir: Path) -> RepoResult:
    """Remove a repository from the registry.

    Resolves *path* to absolute form and removes the matching entry from
    ``~/.autofix/repos.json``.  Returns exit_code 0 even when the path
    was not previously registered (idempotent).
    """
    resolved = str(Path(path).resolve())

    repos_path = _repos_file(home_dir)
    repos = _load_repos(repos_path)

    filtered = [entry for entry in repos if entry.get("path") != resolved]
    _save_repos(repos_path, filtered)

    return RepoResult(
        exit_code=0,
        message=f"Removed repository: {resolved}",
    )


def repo_list(home_dir: Path) -> RepoResult:
    """List all registered repositories, one per line."""
    repos_path = _repos_file(home_dir)
    repos = _load_repos(repos_path)

    lines = [entry["path"] for entry in repos if "path" in entry]
    output = "\n".join(lines)

    return RepoResult(
        exit_code=0,
        output=output,
        message=f"{len(lines)} registered repo(s)",
    )
