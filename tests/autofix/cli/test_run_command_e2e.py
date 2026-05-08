"""End-to-end smoke test for `autofix run` (AC-17.g).

Real `git init` working tree, real scan, real state-machine writes.
LLM seam not exercised (no LLM_PATCH-tier findings expected from a
single-line clean file). Exits 3 (HUMAN_REVIEW) without --apply.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from autofix.cli import run_command


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path, check=True
    )
    (tmp_path / "module.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True
    )
    return tmp_path


def _ns(root: Path, **kw) -> argparse.Namespace:
    base = dict(
        root=root,
        apply=False,
        suggest=False,
        auto_llm=False,
        analyzers="",
        max_retries=3,
        quiet=True,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_bare_run_reaches_human_review_exit_3(
    git_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """Bare `autofix run` (no --apply) walks SCANNING→TRIAGING→PLANNING→HUMAN_REVIEW, exits 3."""
    rc = run_command.run(_ns(git_repo))
    assert rc == 3, f"Expected exit 3 (HUMAN_REVIEW), got {rc}"

    # state.jsonl exists and contains 4 transitions.
    runs_dir = git_repo / ".autofix" / "runs"
    run_dirs = list(runs_dir.iterdir())
    assert len(run_dirs) == 1
    log = (run_dirs[0] / "state.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in log.splitlines() if line.strip()]
    states = [r["to_state"] for r in rows]
    assert states == ["scanning", "triaging", "planning", "human-review"]
    assert rows[-1]["reason"] == "preview_only"


def test_no_source_mutation_under_bare_run(git_repo: Path) -> None:
    """Bare `autofix run` MUST NOT mutate user source."""
    pre = (git_repo / "module.py").read_bytes()
    run_command.run(_ns(git_repo))
    post = (git_repo / "module.py").read_bytes()
    assert pre == post, "Bare `autofix run` must not modify user source"


def test_dispatcher_registers_run_subcommand() -> None:
    """`autofix/cli/main.py` registers the `run` subparser."""
    from autofix.cli.main import _build_parser
    parser = _build_parser()
    # No exception means the subparser was registered.
    args = parser.parse_args(["run", "--root", "/tmp/_autofix_dispatch_check"])
    assert args.subcommand == "run"
    assert callable(args._runner)
