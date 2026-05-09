"""Driver one-cycle smoke tests (ARCH-016 AC-16, AC-17)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch



def _make_repo(tmp_path: Path, files: list[str]) -> Path:
    """Initialize a tmp git repo with seed files."""
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    for f in files:
        (tmp_path / f).write_text(f"# {f}\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_run_crawl_once_empty_ledger_no_findings(tmp_path: Path) -> None:
    """Cycle on a fresh repo with no findings: returns 0, ledger gains rows."""
    from autofix.crawl.driver import run_crawl_once

    _make_repo(tmp_path, ["a.py", "b.py"])

    # Stub the analyzer-dispatch path so this test doesn't hit the real
    # LLM. The driver should still produce ledger rows for each
    # (bundle, analyzer) it picked.
    with patch("autofix.crawl.driver._analyze_bundle") as mock_analyze:
        mock_analyze.return_value = []  # no findings

        rc = run_crawl_once(
            root=tmp_path,
            mode="preview",
            budget="cheap",
            quiet=True,
        )

    assert rc == 0
    log = tmp_path / ".autofix" / "crawl-ledger.jsonl"
    assert log.exists()
    assert log.read_text().strip() != ""


def test_run_crawl_once_records_one_row_per_pair(tmp_path: Path) -> None:
    """For each (bundle, analyzer) emitted by the picker, the driver
    records exactly one ledger row."""
    from autofix.crawl.driver import run_crawl_once

    _make_repo(tmp_path, ["a.py", "b.py", "c.py"])

    with patch("autofix.crawl.driver._analyze_bundle") as mock_analyze:
        mock_analyze.return_value = []
        rc = run_crawl_once(
            root=tmp_path,
            mode="preview",
            budget="cheap",  # 1 bundle/cycle, 2 analyzers
            quiet=True,
        )
    assert rc == 0

    # cheap = 1 bundle × 2 analyzers = 2 ledger rows
    log = (tmp_path / ".autofix" / "crawl-ledger.jsonl").read_text()
    rows = [r for r in log.splitlines() if r.strip()]
    assert len(rows) == 2


def test_preview_mode_does_not_touch_working_tree(tmp_path: Path) -> None:
    """Mode=preview must NOT invoke the apply / verify pipeline."""
    from autofix.crawl.driver import run_crawl_once

    _make_repo(tmp_path, ["a.py"])

    with patch("autofix.crawl.driver._analyze_bundle") as mock_analyze, \
         patch("autofix.crawl.driver._dispatch_repair_workflow") as mock_repair:
        # Even with findings present, preview mode must not call repair.
        mock_analyze.return_value = [
            MagicMock(rule_id="llm:security:secret-leak", path="a.py")
        ]
        rc = run_crawl_once(
            root=tmp_path,
            mode="preview",
            budget="cheap",
            quiet=True,
        )
    assert rc == 0
    mock_repair.assert_not_called()


def test_pr_mode_dispatches_repair_when_findings_present(tmp_path: Path) -> None:
    """Mode=pr with findings → repair workflow is invoked."""
    from autofix.crawl.driver import run_crawl_once

    _make_repo(tmp_path, ["a.py"])

    finding = MagicMock(
        rule_id="llm:security:secret-leak",
        path="a.py",
    )

    with patch("autofix.crawl.driver._analyze_bundle") as mock_analyze, \
         patch("autofix.crawl.driver._dispatch_repair_workflow") as mock_repair:
        mock_analyze.return_value = [finding]
        mock_repair.return_value = 0
        rc = run_crawl_once(
            root=tmp_path,
            mode="pr",
            budget="cheap",
            quiet=True,
        )
    assert rc == 0
    assert mock_repair.called
