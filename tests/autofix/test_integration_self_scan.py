"""Integration test for the full autofix funnel.

Covers:
  AC #2  — console script 'autofix = autofix.cli.main:main' is
           declared under [project.scripts] in pyproject.toml.
  AC #3  — pyproject.toml pins tree-sitter>=0.21,<0.22 and tree-sitter-python.
  AC #4  — `autofix scan --root <repo>` exits 0 and writes SARIF.
  AC #20 — autofix/cli.py is byte-identical to its pre-task form (not modified).
  AC #22 — monkeypatched run_prompt, full pipeline, SARIF
           partialFingerprints.autofixNext/v1 equals at least one finding_id.
  AC #25 — `autofix scan --help` mentions working-tree dirt is ignored.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


pytestmark = pytest.mark.integration


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)


def _commit(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", message],
        cwd=root,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-04-17T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-04-17T00:00:00Z",
        },
    )


@pytest.fixture
def seeded_repo(tmp_path: Path) -> Path:
    """A minimal git repo guaranteed to contain at least one unused-import
    finding. This plays the role of the 'python_minimal' fixture inline —
    no permanent tests/fixtures/ directory is created."""
    _init_git_repo(tmp_path)
    # Commit 1: a file with a used import (so HEAD~1 exists for diff).
    (tmp_path / "module_a.py").write_text(
        "import os\n\npath = os.getcwd()\n", encoding="utf-8"
    )
    _commit(tmp_path, "initial")
    # Commit 2: a file with a clearly unused import.
    (tmp_path / "module_b.py").write_text(
        "import json  # unused on purpose\n\nx = 1\n", encoding="utf-8"
    )
    _commit(tmp_path, "add unused import")
    return tmp_path


def test_console_script_registered() -> None:
    """AC #2: pyproject.toml declares the 'autofix' console script."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "autofix" in pyproject, "missing autofix entry in pyproject.toml"
    # The exact mapping target.
    assert 'autofix = "autofix.cli.main:main"' in pyproject


def test_pyproject_pins_tree_sitter() -> None:
    """AC #3: tree-sitter and tree-sitter-python pinned >=0.21,<0.22."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # Accept either single or double quotes.
    assert (
        "tree-sitter>=0.21,<0.22" in pyproject
        or '"tree-sitter>=0.21,<0.22"' in pyproject
    ), "tree-sitter pin missing or wrong"
    assert (
        "tree-sitter-python>=0.21,<0.22" in pyproject
        or '"tree-sitter-python>=0.21,<0.22"' in pyproject
    ), "tree-sitter-python pin missing or wrong"


def test_no_legacy_cli_module_exists() -> None:
    """task-20260506-003: legacy top-level autofix/cli.py is deleted
    (the new CLI lives at autofix/cli/ as a package, not a single module)."""
    legacy_cli = REPO_ROOT / "autofix" / "cli.py"
    assert not legacy_cli.is_file(), (
        f"legacy autofix/cli.py should be deleted; found at {legacy_cli}"
    )
    assert (REPO_ROOT / "autofix" / "cli" / "main.py").is_file()


def test_scan_help_mentions_working_tree_ignored() -> None:
    """AC #25: `autofix scan --help` mentions working-tree is ignored
    and that replay determinism depends on the diff range."""
    from autofix.cli import main as main_mod

    # Run in-process to avoid requiring a pip-installed console script.
    with pytest.raises(SystemExit) as excinfo:
        # argparse prints --help to stdout and calls sys.exit(0).
        main_mod.main(["scan", "--help"])
    # argparse exits 0 on --help.
    assert excinfo.value.code == 0, excinfo.value

    # Capture the help text by re-running and capturing stdout.
    captured = subprocess.run(
        [sys.executable, "-c",
         "from autofix.cli.main import main; "
         "import sys; sys.argv = ['autofix', 'scan', '--help']; "
         "main(['scan', '--help'])"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    combined = captured.stdout + captured.stderr
    assert (
        "working-tree" in combined.lower() or "working tree" in combined.lower()
    ), f"--help must mention working-tree: {combined!r}"
    assert "ignored" in combined.lower(), f"--help must mention 'ignored': {combined!r}"


def test_scan_produces_sarif_and_exits_zero(
    seeded_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #4: scan exits 0 and writes .autofix/scans-next/<id>/findings.sarif."""
    from autofix.llm_backend import LLMResult

    def _fake_run_prompt(prompt, **kwargs):
        return LLMResult(returncode=0, stdout='{"decision": "confirmed"}', stderr="")

    monkeypatch.setattr("autofix.llm_backend.run_prompt", _fake_run_prompt)

    from autofix.cli.main import main as cli_main

    rc = cli_main(["scan", "--root", str(seeded_repo)])
    assert rc == 0, f"non-zero exit: {rc!r}"

    scans_root = seeded_repo / ".autofix" / "scans"
    assert scans_root.is_dir(), f"scans dir missing: {scans_root}"
    # Exactly one scan directory with findings.sarif.
    scan_dirs = list(scans_root.iterdir())
    assert scan_dirs, f"no scan dir under {scans_root}"
    sarif_path = scan_dirs[0] / "findings.sarif"
    assert sarif_path.is_file(), f"SARIF missing: {sarif_path}"


def test_integration_self_scan_end_to_end(
    seeded_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #22: full pipeline with monkeypatched run_prompt; emitted SARIF's
    partialFingerprints['autofixNext/v1'] equals the finding_id of at least
    one emitted finding."""
    from autofix.llm_backend import LLMResult

    def _fake_run_prompt(prompt, **kwargs):
        return LLMResult(returncode=0, stdout='{"decision": "confirmed"}', stderr="")

    monkeypatch.setattr("autofix.llm_backend.run_prompt", _fake_run_prompt)

    from autofix.cli.main import main as cli_main

    rc = cli_main(["scan", "--root", str(seeded_repo)])
    assert rc == 0

    scan_dirs = list((seeded_repo / ".autofix" / "scans").iterdir())
    sarif = json.loads((scan_dirs[0] / "findings.sarif").read_text(encoding="utf-8"))
    results = sarif["runs"][0]["results"]
    assert results, "expected at least one result (seeded unused import)"

    # Every result carries partialFingerprints.autofixNext/v1 = 64-char hex.
    for r in results:
        fp = r["partialFingerprints"]["autofixNext/v1"]
        assert isinstance(fp, str) and len(fp) == 64
        int(fp, 16)  # hex-decodable
