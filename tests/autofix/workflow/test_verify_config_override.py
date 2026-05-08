"""Config-file override path (ARCH-011 AC-8)."""
from __future__ import annotations

import json
from pathlib import Path

from autofix.workflow.verify import _resolve_test_command


def test_config_command_overrides_detection(tmp_path: Path) -> None:
    """If `.autofix/config.json` provides a test command, that command
    is used regardless of which marker file is present."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    cfg_dir = tmp_path / ".autofix"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"test": {"command": ["custom", "runner", "--flag"]}})
    )

    runner, command, timeout, cwd = _resolve_test_command(tmp_path)

    assert command == ("custom", "runner", "--flag")
    assert runner == "configured"


def test_config_timeout_override(tmp_path: Path) -> None:
    cfg_dir = tmp_path / ".autofix"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {"test": {"command": ["pytest"], "timeout_seconds": 42}}
        )
    )

    _, _, timeout, _ = _resolve_test_command(tmp_path)
    assert timeout == 42


def test_config_default_timeout_when_unspecified(tmp_path: Path) -> None:
    cfg_dir = tmp_path / ".autofix"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"test": {"command": ["pytest"]}})
    )

    from autofix.workflow.verify_constants import DEFAULT_TIMEOUT_SECONDS

    _, _, timeout, _ = _resolve_test_command(tmp_path)
    assert timeout == DEFAULT_TIMEOUT_SECONDS


def test_config_cwd_override_resolved(tmp_path: Path) -> None:
    sub = tmp_path / "subproject"
    sub.mkdir()
    cfg_dir = tmp_path / ".autofix"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {"test": {"command": ["pytest"], "cwd": "subproject"}}
        )
    )

    _, _, _, cwd = _resolve_test_command(tmp_path)
    assert cwd == sub.resolve()


def test_malformed_config_falls_back_to_detection(tmp_path: Path) -> None:
    """Bad JSON in config.json must NOT mask marker-based detection."""
    (tmp_path / "go.mod").write_text("module x\n")
    cfg_dir = tmp_path / ".autofix"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("{not valid json")

    runner, command, _, _ = _resolve_test_command(tmp_path)
    assert runner == "go-test"
    assert command == ("go", "test", "./...")


def test_config_with_empty_command_list_falls_back(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    cfg_dir = tmp_path / ".autofix"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"test": {"command": []}})
    )

    runner, command, _, _ = _resolve_test_command(tmp_path)
    # Empty list is rejected as a valid override; detection wins.
    assert runner == "pytest"
    assert command == ("pytest", "-x")
