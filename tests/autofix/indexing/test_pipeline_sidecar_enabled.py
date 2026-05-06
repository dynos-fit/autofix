"""task-012 AC 27 — running the pipeline with the sidecar enabled produces
at least one ``EmbeddingSidecarColdRebuild`` or
``EmbeddingSidecarIncrementalUpdate`` event per scan.

Skipped when sentence-transformers / hnswlib are not importable — the
enabled path cannot exercise its live event emissions without the deps.
When deps are absent the degraded path is covered by
``test_embedding_sidecar.py::test_enabled_with_missing_deps_emits_degraded_event``.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest


def _deps_available() -> bool:
    try:
        importlib.import_module("sentence_transformers")
        importlib.import_module("hnswlib")
    except ImportError:
        return False
    return True


HAVE_DEPS = _deps_available()


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=root, check=True
    )


def _commit(root: Path, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", msg],
        cwd=root,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-04-17T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-04-17T00:00:00Z",
        },
    )


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    (tmp_path / "module_a.py").write_text(
        "import os\n\npath = os.getcwd()\n", encoding="utf-8"
    )
    _commit(tmp_path, "init")
    (tmp_path / "module_b.py").write_text(
        "import json  # unused\n\nx = 1\n", encoding="utf-8"
    )
    _commit(tmp_path, "add unused")
    return tmp_path


@pytest.mark.skipif(
    not HAVE_DEPS,
    reason="enabled-path event emissions require sentence-transformers + hnswlib",
)
def test_pipeline_emits_sidecar_event_when_enabled(tiny_repo: Path) -> None:
    from autofix.events.schema import ChangeSet
    from autofix.funnel.pipeline import run_scan
    from autofix.indexing.embedding import EmbeddingSidecar

    changeset = ChangeSet(
        paths=("module_b.py",), watcher_confidence="diff-head1"
    )
    # Seed the sidecar so the scan sees a non-empty recall stage AND the
    # incremental-or-coldrebuild event contract still holds on a clean run.
    sidecar = EmbeddingSidecar(
        tiny_repo,
        {"index": {"embedding_sidecar": {"enabled": True}}},
    )
    sidecar.cold_rebuild(
        [
            {
                "symbol_id": "sym_seed",
                "text": "python::unused_import import json",
                "symbol_name": "unused_import",
                "language": "python",
                "path": "module_b.py",
            }
        ]
    )
    run_scan(
        tiny_repo,
        changeset,
        scan_id="scan_enabled",
        policy={"index": {"embedding_sidecar": {"enabled": True}}},
    )

    events = [
        json.loads(line)
        for line in (tiny_repo / ".autofix" / "events.jsonl")
        .read_text()
        .splitlines()
        if line
    ]
    sidecar_events = [
        e
        for e in events
        if e.get("event")
        in {
            "EmbeddingSidecarColdRebuild",
            "EmbeddingSidecarIncrementalUpdate",
        }
    ]
    assert sidecar_events, (
        "expected at least one EmbeddingSidecarColdRebuild OR "
        "EmbeddingSidecarIncrementalUpdate event"
    )
