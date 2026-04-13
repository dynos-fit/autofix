from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
from unittest.mock import patch

from autofix.app import cmd_scan


def _setup_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    (path / ".autofix").mkdir()
    return path


def test_cmd_scan_allows_openai_backend_without_claude(tmp_path: Path) -> None:
    repo = _setup_repo(tmp_path / "repo")
    (repo / ".autofix" / "config.json").write_text(
        json.dumps(
            {
                "llm_backend": "openai_compatible",
                "llm_base_url": "http://127.0.0.1:11434/v1",
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(root=str(repo), dry_run=False, max_findings=3)

    with (
        patch("autofix.app.shutil.which", return_value=None),
        patch("autofix.app.scan_lock", return_value=nullcontext()) as mock_scan_lock,
        patch("autofix.app.scan_locked", return_value=0) as mock_scan_locked,
    ):
        exit_code = cmd_scan(args)

    assert exit_code == 0
    mock_scan_lock.assert_called_once_with(repo)
    mock_scan_locked.assert_called_once()


def test_cmd_scan_requires_claude_for_claude_backend(tmp_path: Path) -> None:
    repo = _setup_repo(tmp_path / "repo")
    args = argparse.Namespace(root=str(repo), dry_run=False, max_findings=3)

    with (
        patch("autofix.app.shutil.which", return_value=None),
        patch("autofix.app.scan_locked") as mock_scan_locked,
    ):
        exit_code = cmd_scan(args)

    assert exit_code == 1
    mock_scan_locked.assert_not_called()


def test_cmd_scan_does_not_write_manifest_when_lock_fails(tmp_path: Path) -> None:
    repo = _setup_repo(tmp_path / "repo")
    args = argparse.Namespace(root=str(repo), dry_run=False, max_findings=3)

    with (
        patch("autofix.app.scan_lock", side_effect=RuntimeError("scan already running")),
        patch("autofix.app.write_scan_artifact") as mock_write_scan_artifact,
    ):
        exit_code = cmd_scan(args)

    assert exit_code == 1
    mock_write_scan_artifact.assert_not_called()
