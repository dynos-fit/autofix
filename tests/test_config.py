"""Tests for autofix.config module.

Covers acceptance criteria: 8 (parse_interval), 17 (config show), 18 (config set).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autofix.config import (
    SUPPORTED_KEYS,
    config_set,
    config_show,
    parse_interval,
    resolve_config,
)


def _setup_repo(path: Path) -> Path:
    """Create a repo with .autofix/ directory."""
    autofix_dir = path / ".autofix"
    autofix_dir.mkdir(parents=True)
    return path


# ---------------------------------------------------------------------------
# Criterion 8: parse_interval
# ---------------------------------------------------------------------------

class TestParseInterval:
    """Criterion 8: parse_interval converts '15m', '2h' to seconds."""

    def test_minutes_suffix(self) -> None:
        assert parse_interval("15m") == 900

    def test_hours_suffix(self) -> None:
        assert parse_interval("2h") == 7200

    def test_30m_default(self) -> None:
        assert parse_interval("30m") == 1800

    def test_1h(self) -> None:
        assert parse_interval("1h") == 3600

    def test_invalid_suffix_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_interval("5d")

    def test_non_numeric_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_interval("abc")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_interval("")

    def test_zero_minutes(self) -> None:
        """Zero interval should either raise or return 0 -- either is acceptable."""
        # Implementation may choose to reject 0 as invalid
        result = parse_interval("0m")
        assert result == 0

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_interval("-5m")


# ---------------------------------------------------------------------------
# Criterion 17: config show
# ---------------------------------------------------------------------------

class TestConfigShow:
    """Criterion 17: config show prints resolved config (defaults + overrides)."""

    def test_show_returns_defaults_when_no_overrides(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        result = config_show(root=repo)
        assert result.exit_code == 0
        # Output should contain known default keys
        assert "max_files" in result.output or "scan_timeout" in result.output

    def test_show_includes_per_repo_overrides(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        config_file = repo / ".autofix" / "config.json"
        config_file.write_text(json.dumps({"max_files": 42}))
        result = config_show(root=repo)
        assert "42" in result.output

    def test_show_json_flag_produces_valid_json(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        result = config_show(root=repo, as_json=True)
        parsed = json.loads(result.output)
        assert isinstance(parsed, dict)
        assert "max_files" in parsed or "scan_timeout" in parsed


# ---------------------------------------------------------------------------
# Criterion 18: config set
# ---------------------------------------------------------------------------

class TestConfigSet:
    """Criterion 18: config set updates a single key in .autofix/config.json."""

    def test_set_valid_key(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        result = config_set(root=repo, key="max_files", value="12")
        assert result.exit_code == 0
        config = json.loads((repo / ".autofix" / "config.json").read_text())
        assert config["max_files"] == 12

    def test_set_interval_key(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        result = config_set(root=repo, key="interval", value="15m")
        assert result.exit_code == 0
        config = json.loads((repo / ".autofix" / "config.json").read_text())
        assert config["interval"] == "15m"

    def test_set_dry_run_bool(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        result = config_set(root=repo, key="dry_run", value="true")
        assert result.exit_code == 0
        config = json.loads((repo / ".autofix" / "config.json").read_text())
        assert config["dry_run"] is True

    def test_set_min_confidence_float(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        result = config_set(root=repo, key="min_confidence", value="0.8")
        assert result.exit_code == 0
        config = json.loads((repo / ".autofix" / "config.json").read_text())
        assert config["min_confidence"] == 0.8

    def test_set_invalid_key_fails(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        result = config_set(root=repo, key="nonexistent_key", value="42")
        assert result.exit_code == 1
        # Error should list valid keys
        for key in SUPPORTED_KEYS:
            assert key in result.message

    def test_set_preserves_existing_keys(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        config_file = repo / ".autofix" / "config.json"
        config_file.write_text(json.dumps({"max_files": 8}))
        config_set(root=repo, key="interval", value="1h")
        config = json.loads(config_file.read_text())
        assert config["max_files"] == 8
        assert config["interval"] == "1h"

    def test_set_overwrites_existing_key(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        config_set(root=repo, key="max_files", value="8")
        config_set(root=repo, key="max_files", value="16")
        config = json.loads((repo / ".autofix" / "config.json").read_text())
        assert config["max_files"] == 16


# ---------------------------------------------------------------------------
# Criterion 17/18: resolve_config (defaults merge)
# ---------------------------------------------------------------------------

class TestResolveConfig:
    """Criterion 17/18: resolve_config merges defaults.py with per-repo overrides."""

    def test_defaults_used_when_no_config_json(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        config = resolve_config(root=repo)
        assert isinstance(config, dict)
        # Should have default values for known keys
        assert "scan_timeout" in config
        assert config["scan_timeout"] > 0

    def test_overrides_take_precedence(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        config_file = repo / ".autofix" / "config.json"
        config_file.write_text(json.dumps({"max_files": 99}))
        config = resolve_config(root=repo)
        assert config["max_files"] == 99

    def test_defaults_fill_missing_keys(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path / "repo")
        config_file = repo / ".autofix" / "config.json"
        config_file.write_text(json.dumps({"max_files": 99}))
        config = resolve_config(root=repo)
        # Keys not in config.json should come from defaults
        assert "scan_timeout" in config


# ---------------------------------------------------------------------------
# Supported keys validation
# ---------------------------------------------------------------------------

class TestSupportedKeys:
    """Criterion 18: validate the set of supported config keys."""

    def test_supported_keys_contains_all_spec_keys(self) -> None:
        expected = {
            "max_files",
            "interval",
            "max_findings",
            "scan_timeout",
            "llm_timeout",
            "min_confidence",
            "max_open_prs",
            "max_prs_per_day",
            "review_model",
            "dry_run",
        }
        assert expected == set(SUPPORTED_KEYS)
