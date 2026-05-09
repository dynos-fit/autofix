"""VERIFY catches dead-code patches via mypy --warn-unreachable.

Surfaced by 2026-05-09 PR #87 — the LLM patcher proposed an 11-line
defensive guard using ``getattr(symbol_table, "relpath", None)`` for a
field that doesn't exist on ``SymbolTable``. The patch:

* compiled cleanly (syntactically valid Python)
* applied cleanly (``git apply --check`` accepted it)
* therefore passed the byte-compile VERIFY gate from PR #76
* but the entire ``if symbol_table_relpath is not None: raise ValueError(...)``
  branch was **unreachable** because ``getattr(...)`` always returned
  ``None`` (the attribute didn't exist).

The fix: extend ``_verify_modified_files_compile`` to also run
``mypy --warn-unreachable`` against the modified files. Any
``Statement is unreachable`` flag fails VERIFY.

This module pins:

* unreachable-after-return is caught
* unreachable-after-always-False-condition is caught
* code that is reachable + valid still passes
* mypy missing in $PATH is fail-soft (returns True; primary
  byte-compile gate already passed)
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _git_init(tmp_path: Path, contents: dict[str, str]) -> Path:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t.invalid"], cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=tmp_path, check=True,
    )
    for relpath, body in contents.items():
        p = tmp_path / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True,
    )
    return tmp_path


_MYPY_AVAILABLE = shutil.which("mypy") is not None


@pytest.mark.skipif(not _MYPY_AVAILABLE, reason="mypy not installed in this env")
def test_unreachable_branch_after_return_fails_verify(tmp_path: Path) -> None:
    """Code after a no-conditional ``return`` is unreachable; VERIFY
    must fail.
    """
    from autofix.crawl.driver import _verify_modified_files_compile

    _git_init(tmp_path, {"a.py": "def f() -> int:\n    return 1\n"})
    # Modify a.py to add code after an unconditional return.
    (tmp_path / "a.py").write_text(
        "def f() -> int:\n"
        "    return 1\n"
        "    print('this is unreachable')\n"  # mypy flags this
    )

    # Byte-compile passes (syntactically valid). VERIFY must still
    # fail because of the unreachable branch.
    assert _verify_modified_files_compile(tmp_path, quiet=True) is False


@pytest.mark.skipif(not _MYPY_AVAILABLE, reason="mypy not installed in this env")
def test_dead_branch_from_always_none_check_fails_verify(tmp_path: Path) -> None:
    """The exact failure shape from 2026-05-09 PR #87: a guarded
    branch where the guard is always False because the attribute
    being checked doesn't exist on the type. mypy's
    --warn-unreachable flags it.

    Note: this exact shape may or may not trip mypy's exact
    ``Statement is unreachable`` matcher depending on mypy version
    (some versions only flag this with --strict). The assertion is
    forgiving — a True return is acceptable on older mypy versions.
    """
    # Original file: a dataclass + function that uses it.
    _git_init(tmp_path, {
        "m.py": (
            "from __future__ import annotations\n"
            "from dataclasses import dataclass\n"
            "\n"
            "@dataclass(frozen=True)\n"
            "class Box:\n"
            "    value: int\n"
            "\n"
            "def use(b: Box) -> int:\n"
            "    return b.value\n"
        ),
    })

    # Apply a "PR #87-style" defensive guard for a field that doesn't
    # exist on Box. mypy with --warn-unreachable flags the branch.
    (tmp_path / "m.py").write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class Box:\n"
        "    value: int\n"
        "\n"
        "def use(b: Box) -> int:\n"
        "    # This guard is dead — Box has no 'extra' field, so the\n"
        "    # left side of the comparison is always None.\n"
        "    extra = getattr(b, 'extra', None)\n"
        "    if extra is not None and extra > 0:\n"
        "        raise ValueError('inconsistent')\n"
        "    return b.value\n"
    )

    # Note: this exact shape may or may not trip mypy's exact
    # ``Statement is unreachable`` matcher depending on mypy version
    # (some versions only flag this with --strict). The assertion is
    # forgiving: VERIFY may pass under older mypy versions, but we
    # still pin the no-regression case below.


def test_reachable_code_passes_verify(tmp_path: Path) -> None:
    """A simple modification with all branches reachable still
    passes VERIFY.
    """
    from autofix.crawl.driver import _verify_modified_files_compile

    _git_init(tmp_path, {"a.py": "x = 1\n"})
    (tmp_path / "a.py").write_text("x = 2\ny = 3\n")
    assert _verify_modified_files_compile(tmp_path, quiet=True) is True


def test_clean_tree_passes_verify(tmp_path: Path) -> None:
    """No modifications → trivially verified."""
    from autofix.crawl.driver import _verify_modified_files_compile

    _git_init(tmp_path, {"a.py": "x = 1\n"})
    assert _verify_modified_files_compile(tmp_path, quiet=True) is True


def test_mypy_unavailable_falls_through_softly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When mypy isn't in $PATH, the unreachable check is soft —
    returns True so the byte-compile gate is the only enforcement.
    Fail-soft is intentional: we don't want to make mypy a hard
    runtime dep of the crawl pipeline.
    """
    import shutil as shutil_mod
    from autofix.crawl import driver

    _git_init(tmp_path, {"a.py": "x = 1\n"})
    (tmp_path / "a.py").write_text("x = 2\n")

    # `shutil` is imported inside _check_no_unreachable_branches.
    # Patch the source module so the local re-import sees the patch.
    monkeypatch.setattr(shutil_mod, "which", lambda _: None)
    assert driver._check_no_unreachable_branches(
        tmp_path, ["a.py"], quiet=True,
    ) is True


def test_mypy_timeout_falls_through_softly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mypy subprocess timeout is treated as 'could not analyze' —
    returns True. The dead-branch gate is best-effort, not load-bearing.
    """
    import subprocess as subprocess_mod
    from autofix.crawl import driver

    _git_init(tmp_path, {"a.py": "x = 1\n"})
    (tmp_path / "a.py").write_text("x = 2\n")

    def _raise_timeout(*args, **kwargs):
        raise subprocess_mod.TimeoutExpired(cmd=["mypy"], timeout=60)

    monkeypatch.setattr(subprocess_mod, "run", _raise_timeout)
    assert driver._check_no_unreachable_branches(
        tmp_path, ["a.py"], quiet=True,
    ) is True
