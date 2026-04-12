"""Implements `autofix init` — repo bootstrapping and registration."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from autofix.repo import _load_repos, _save_repos
from autofix.state import default_autofix_policy


REQUIRED_TOOLS = ("git", "gh")


@dataclass
class InitResult:
    """Result of cmd_init."""

    exit_code: int
    message: str


def _check_prerequisites() -> list[str]:
    """Return list of required CLI tools not found on PATH."""
    missing: list[str] = []
    for tool in REQUIRED_TOOLS:
        if shutil.which(tool) is None:
            missing.append(tool)
    return missing


def cmd_init(
    root: Path,
    *,
    home_dir: Path | None = None,
    max_files: int | None = None,
    interval: str | None = None,
) -> InitResult:
    """Bootstrap a repo for autofix.

    Parameters
    ----------
    root:
        Path to the git repository root.
    home_dir:
        Override for the user home directory (used by tests).
    max_files:
        Optional per-repo max files override.
    interval:
        Optional per-repo scan interval override.
    """
    # --- prerequisite checks ------------------------------------------------
    missing = _check_prerequisites()
    if missing:
        names = ", ".join(missing)
        return InitResult(
            exit_code=1,
            message=f"Missing required tool(s): {names}",
        )

    # --- resolve paths ------------------------------------------------------
    resolved_root = root.resolve()
    if home_dir is None:
        home = Path.home()
    else:
        home = home_dir

    autofix_dir = resolved_root / ".autofix"
    home_autofix_dir = home / ".autofix"
    repos_file = home_autofix_dir / "repos.json"

    # --- check if already initialized --------------------------------------
    autofix_dir = resolved_root / ".autofix"
    policy_file = autofix_dir / "autofix-policy.json"
    already_initialized = autofix_dir.is_dir() and policy_file.is_file()
    has_overrides = max_files is not None or interval is not None

    if already_initialized and not has_overrides:
        return InitResult(
            exit_code=0,
            message="Already initialized. Use --max-files or --interval to update settings.",
        )

    # --- create .autofix/ in repo ------------------------------------------
    autofix_dir.mkdir(parents=True, exist_ok=True)

    # --- write default policy (idempotent: skip if exists) -----------------
    if not policy_file.exists():
        policy = default_autofix_policy()
        policy_file.write_text(json.dumps(policy, indent=2))

    # --- register repo in ~/.autofix/repos.json ----------------------------
    home_autofix_dir.mkdir(parents=True, exist_ok=True)
    repos = _load_repos(repos_file)
    repo_path_str = str(resolved_root)
    already_registered = any(
        entry.get("path") == repo_path_str for entry in repos
    )
    if not already_registered:
        repos.append({"path": repo_path_str})
        _save_repos(repos_file, repos)

    # --- per-repo config overrides -----------------------------------------
    config_file = autofix_dir / "config.json"

    if has_overrides:
        existing_config: dict = {}
        if config_file.exists():
            try:
                existing_config = json.loads(config_file.read_text())
            except (json.JSONDecodeError, OSError):
                existing_config = {}
            if not isinstance(existing_config, dict):
                existing_config = {}

        if max_files is not None:
            existing_config["max_files"] = max_files
        if interval is not None:
            existing_config["interval"] = interval

        config_file.write_text(json.dumps(existing_config, indent=2))
        return InitResult(exit_code=0, message="Updated autofix settings.")

    return InitResult(exit_code=0, message="Initialized autofix for this repository.")
