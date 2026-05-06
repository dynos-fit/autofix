"""Session-scoped fixtures for the SCIP-index test suite (AC #21).

The ``synthetic_50k_loc_repo`` fixture materializes a 1000-file
hub-and-spoke Python repo (~50 LOC per file, ~50k LOC total) under the
pytest-managed ``tmp_path_factory`` root so generation cost is paid once
per pytest session and is NOT persisted across sessions (no on-disk
cache outside the pytest tmp root).

The synthesizer mirrors task-003's ``test_planner_algorithmic.py``
hub-and-spoke pattern: one ``hub.py`` defines ``hub_func``; each of the
1000 leaves imports ``hub_func`` and defines its own ``leaf_func_i``.
Every leaf is padded to roughly 50 source lines with filler statements so
the total LOC budget (~50k) is hit without altering call-graph shape.

After file generation the fixture runs ``git init && git add . && git
commit`` inside the tmp directory so ``CallGraph.build_from_root``'s
``git ls-files`` branch is exercised.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_N_LEAVES = 1000
_FILLER_LINES_PER_LEAF = 44  # brings total per-leaf to ~50 LOC


def _filler_block(index: int, n_lines: int) -> str:
    """Return ``n_lines`` of trivial, syntactically-valid filler code.

    Each filler line is a unique local-variable assignment so the file
    stays parseable and has a deterministic, indexable shape. We avoid
    function/class defs here so the call-graph shape stays hub-and-spoke.
    """

    return "\n".join(
        f"_filler_{index}_{i} = {i}" for i in range(n_lines)
    )


def _write_synthetic_repo(root: Path) -> None:
    """Materialize the 1000-file hub-and-spoke repo under ``root``.

    Layout::

        hub.py           # defines hub_func
        leaf_0.py ...    # imports hub_func, defines leaf_func_i + filler
        leaf_999.py
    """

    (root / "hub.py").write_text(
        "def hub_func():\n    return 1\n",
        encoding="utf-8",
    )
    for i in range(_N_LEAVES):
        leaf = root / f"leaf_{i}.py"
        body = (
            "from hub import hub_func\n"
            "\n"
            f"def leaf_func_{i}():\n"
            f"    return hub_func()\n"
            "\n"
            + _filler_block(i, _FILLER_LINES_PER_LEAF)
            + "\n"
        )
        leaf.write_text(body, encoding="utf-8")


def _git_init_and_commit(root: Path) -> None:
    """Run ``git init && git add . && git commit`` inside ``root``.

    Uses explicit author/email env to avoid failure on CI runners that
    have no ``user.email`` configured. Swallows no errors — if git is
    unavailable the fixture should fail loudly so the test suite doesn't
    silently fall back to the os.walk branch and obscure a real regression.
    """

    env = {
        "GIT_AUTHOR_NAME": "synthetic-fixture",
        "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
        "GIT_COMMITTER_NAME": "synthetic-fixture",
        "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
    }
    subprocess.run(
        ["git", "init", "-q"], cwd=str(root), check=True, env={**env}
    )
    subprocess.run(
        ["git", "add", "."], cwd=str(root), check=True, env={**env}
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "synthetic fixture commit"],
        cwd=str(root),
        check=True,
        env={**env},
    )


@pytest.fixture(scope="session")
def synthetic_50k_loc_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped 1000-file hub-and-spoke repo (AC #21).

    Returns the absolute :class:`Path` to the fixture root. The fixture
    is generated exactly once per pytest session and torn down by the
    pytest tmp-factory's standard cleanup — there is no on-disk cache
    outside the pytest tmp root.
    """

    root = tmp_path_factory.mktemp("synth-50k-loc")
    _write_synthetic_repo(root)
    _git_init_and_commit(root)
    return root
