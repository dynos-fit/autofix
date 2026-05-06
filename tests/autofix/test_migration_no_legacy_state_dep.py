"""TDD-first: assert migration.py is independent of legacy autofix.state.

Covers:
- AC 14: autofix/migration.py reads findings.json via stdlib, returns []
  on missing/malformed, invokes log with substring 'could not load
  findings file' on parse failures, and does NOT import any module
  under a now-deleted legacy path.

These tests MUST FAIL until task-20260506-003 lands.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_migration_module_does_not_import_legacy_state() -> None:
    """AC 14: autofix/migration.py source contains no `import autofix.state`
    nor `from autofix import state` reference."""
    import autofix

    pkg_root = Path(autofix.__file__).resolve().parent
    migration = pkg_root / "migration.py"
    assert migration.is_file(), f"migration.py not found at {migration}"
    text = migration.read_text(encoding="utf-8")
    # The legacy state.py module is deleted; migration.py must not reference it.
    assert "import autofix.state" not in text, (
        "migration.py still imports autofix.state"
    )
    assert "from autofix.state" not in text, (
        "migration.py still references autofix.state"
    )
    assert "from autofix import state" not in text, (
        "migration.py still imports the legacy state module"
    )


def test_load_legacy_findings_missing_file_returns_empty(tmp_path: Path) -> None:
    """AC 14: missing findings.json -> [] without raising."""
    from autofix.migration import load_legacy_findings

    out = load_legacy_findings(tmp_path)
    assert out == []


def test_load_legacy_findings_malformed_logs_and_returns_empty(tmp_path: Path) -> None:
    """AC 14: malformed JSON -> [] + log invoked with 'could not load findings file'."""
    from autofix.migration import load_legacy_findings

    state_dir = tmp_path / ".autofix" / "state" / "current"
    state_dir.mkdir(parents=True)
    (state_dir / "findings.json").write_text("{ this is not valid JSON ")

    logs: list[str] = []
    out = load_legacy_findings(tmp_path, log=logs.append)
    assert out == []
    assert any("could not load findings file" in m for m in logs), (
        f"expected 'could not load findings file' substring in logs, got: {logs!r}"
    )
