"""Log file is persisted under log_dir with attempt-templated name (AC-13)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autofix.workflow.verify import run_verification


def _stub_scan() -> object:
    return SimpleNamespace(
        exit_code=0,
        findings=[],
        sarif_path=None,
        scan_id="test",
        watcher_confidence=None,
    )


def test_log_path_uses_attempt_in_filename(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    cfg = tmp_path / ".autofix"
    cfg.mkdir()
    cfg.joinpath("config.json").write_text(
        '{"test": {"command": ["python3", "-c", "import sys; sys.exit(0)"]}}'
    )
    log_dir = tmp_path / "runs" / "abc"
    log_dir.mkdir(parents=True)

    with patch("autofix.workflow.verify._run_scan_core", return_value=_stub_scan()):
        result = run_verification(
            root=tmp_path,
            analyzer_set=None,
            applied_finding_ids=frozenset(),
            pre_finding_ids=frozenset(),
            log_dir=log_dir,
            attempt=2,
        )

    assert result.test_log_path is not None
    assert result.test_log_path.name == "verify-2.log"
    assert result.test_log_path.parent == log_dir
    assert result.test_log_path.exists()


def test_log_dir_auto_created(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    cfg = tmp_path / ".autofix"
    cfg.mkdir()
    cfg.joinpath("config.json").write_text(
        '{"test": {"command": ["python3", "-c", "import sys; sys.exit(0)"]}}'
    )
    log_dir = tmp_path / "does" / "not" / "exist"
    # Do NOT mkdir — verify must auto-create.

    with patch("autofix.workflow.verify._run_scan_core", return_value=_stub_scan()):
        result = run_verification(
            root=tmp_path,
            analyzer_set=None,
            applied_finding_ids=frozenset(),
            pre_finding_ids=frozenset(),
            log_dir=log_dir,
            attempt=1,
        )

    assert result.test_log_path is not None
    assert result.test_log_path.exists()
    assert log_dir.exists()
