"""Pure-function unit tests for the safety-check helpers in fix_command.

Covers AC-9, AC-10, AC-11. No filesystem or git I/O.

Imports the private helpers directly:
  _noqa_suppressed  — re-based noqa-line detector
  _SAFE_IMPORT_RE   — compiled regex for single-name import shapes
  _classify_line    — classifies a line as "safe" | "unsafe-multiname"
  _is_dirty_tree    — parses git-status --porcelain output (subprocess.run
                       is monkeypatched to avoid real git I/O)
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Lazy import: fix_command.py does not exist yet; all tests in this module
# are collected but will fail with ImportError until seg-1 ships.
# ---------------------------------------------------------------------------

def _import_fix_command():
    from autofix.cli import fix_command  # noqa: PLC0415
    return fix_command


# ---------------------------------------------------------------------------
# AC-9  _noqa_suppressed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "line_text, expected",
    [
        # Truthy — several noqa variants
        ("import os  # noqa", True),
        ("import os  # noqa: F401", True),
        ("import os  # NOQA: F401, E501", True),
        ("import os  #noqa", True),
        ("import os  #NOQA", True),
        ("import os  #  noqa", True),
        ("import os  # noqa:F401", True),
        # Falsy — no noqa marker
        ("import os", False),
        ("import os  # comment", False),
        ("import os  # not a suppression", False),
        ("import os  # nota bene", False),
        # Edge: noqa in middle of comment word does NOT match (word boundary
        # is enforced by \b in the regex)
        # "noqa" as a substring of another word would not match — we test
        # the plain case instead
        ("import os  # noqua: F401", False),
    ],
)
def test_noqa_suppressed(line_text: str, expected: bool) -> None:
    """AC-9: _noqa_suppressed returns True iff line carries a noqa marker."""
    fc = _import_fix_command()
    result = fc._noqa_suppressed(line_text)
    assert bool(result) is expected, (
        f"_noqa_suppressed({line_text!r}) = {result!r}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# AC-10  _SAFE_IMPORT_RE  (direct regex tests)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "line_text",
    [
        # Bare import, single name
        "import os",
        "import os.path",
        "import os as operating_system",
        # from … import …
        "from pathlib import Path",
        "from pathlib import Path as P",
        "from os.path import join",
        "from os.path import join as j",
        # Leading whitespace (indented import in function body)
        "    import os",
        "    from pathlib import Path",
        "    import os as operating_system",
        "    from x import y as z",
        # Trailing comment
        "import os  # unused",
        "from pathlib import Path  # needed for type hints",
        "import os as operating_system  # comment",
        "from x import y  # comment",
    ],
)
def test_safe_import_re_matches(line_text: str) -> None:
    """AC-10: _SAFE_IMPORT_RE matches all single-name import shapes."""
    fc = _import_fix_command()
    assert fc._SAFE_IMPORT_RE.match(line_text), (
        f"_SAFE_IMPORT_RE should match {line_text!r} but did not"
    )


@pytest.mark.parametrize(
    "line_text",
    [
        # Comma-separated names — must NOT match
        "import os, sys",
        "import a, b",
        "from x import a, b",
        "from x import y, z",
        # Parenthesized import list
        "from x import (a, b)",
        "from x import (a,",
        "from x import (",
        # Multi-name with trailing comment
        "import os, sys  # both unused",
        # Blank line / non-import
        "",
        "x = 1",
        "def foo():",
    ],
)
def test_safe_import_re_does_not_match(line_text: str) -> None:
    """AC-10: _SAFE_IMPORT_RE does NOT match multi-name or non-import lines."""
    fc = _import_fix_command()
    assert not fc._SAFE_IMPORT_RE.match(line_text), (
        f"_SAFE_IMPORT_RE should NOT match {line_text!r} but it did"
    )


# ---------------------------------------------------------------------------
# AC-10  _classify_line (safe / unsafe-multiname)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "start_line, end_line, line_text, expected",
    [
        # Safe single-name imports (same start == end line)
        (1, 1, "import os", "safe"),
        (1, 1, "import os as operating_system", "safe"),
        (1, 1, "from pathlib import Path", "safe"),
        (1, 1, "from pathlib import Path as P", "safe"),
        (1, 1, "    import os  # trailing comment", "safe"),
        (1, 1, "from os.path import join", "safe"),
        (5, 5, "import json", "safe"),
        (3, 3, "from x import y as z", "safe"),
        # Unsafe: multi-name on a single line
        (1, 1, "from x import a, b", "unsafe-multiname"),
        (1, 1, "from x import (a, b)", "unsafe-multiname"),
        (1, 1, "import a, b", "unsafe-multiname"),
        (1, 1, "import os, sys", "unsafe-multiname"),
        # Unsafe: multi-line range (start_line != end_line)
        (1, 3, "from x import (", "unsafe-multiname"),
        (2, 5, "from x import (", "unsafe-multiname"),
        (1, 2, "import os", "unsafe-multiname"),
    ],
)
def test_classify_line(
    start_line: int, end_line: int, line_text: str, expected: str
) -> None:
    """AC-10: _classify_line returns 'safe' or 'unsafe-multiname' correctly."""
    fc = _import_fix_command()
    result = fc._classify_line(start_line, end_line, line_text)
    assert result == expected, (
        f"_classify_line({start_line}, {end_line}, {line_text!r}) = "
        f"{result!r}, expected {expected!r}"
    )


def test_classify_line_multiline_range_overrides_safe_pattern() -> None:
    """AC-10: even if a line matches the regex, multi-line range -> unsafe."""
    fc = _import_fix_command()
    # 'import os' matches _SAFE_IMPORT_RE but start_line != end_line
    result = fc._classify_line(1, 2, "import os")
    assert result == "unsafe-multiname"


# ---------------------------------------------------------------------------
# AC-11  _is_dirty_tree  (monkeypatched subprocess.run)
# ---------------------------------------------------------------------------

def _make_completed_process(stdout: str) -> subprocess.CompletedProcess:
    """Build a CompletedProcess stub with the given stdout."""
    return subprocess.CompletedProcess(
        args=["git", "status", "--porcelain"],
        returncode=0,
        stdout=stdout,
        stderr="",
    )


def test_is_dirty_tree_empty_output_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-11: empty git status porcelain -> (False, 0)."""
    fc = _import_fix_command()
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _make_completed_process(""),
    )
    dirty, count = fc._is_dirty_tree(Path("."))
    assert dirty is False
    assert count == 0


def test_is_dirty_tree_only_untracked_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-11: only '??' untracked lines -> (False, 0)."""
    fc = _import_fix_command()
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _make_completed_process("?? newfile.py\n?? another.py\n"),
    )
    dirty, count = fc._is_dirty_tree(Path("."))
    assert dirty is False
    assert count == 0


def test_is_dirty_tree_modified_file_is_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-11: a modified tracked file -> (True, 1)."""
    fc = _import_fix_command()
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _make_completed_process("M  file.py\n"),
    )
    dirty, count = fc._is_dirty_tree(Path("."))
    assert dirty is True
    assert count == 1


def test_is_dirty_tree_mixed_untracked_and_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-11: mix of '??' untracked and modified lines -> (True, N) where N
    counts only the non-'??' lines."""
    fc = _import_fix_command()
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _make_completed_process(
            "M  a.py\n?? b.py\n?? c.py\n"
        ),
    )
    dirty, count = fc._is_dirty_tree(Path("."))
    assert dirty is True
    assert count == 1


def test_is_dirty_tree_multiple_dirty_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-11: multiple modified/deleted files -> (True, N)."""
    fc = _import_fix_command()
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _make_completed_process(
            " M staged_modified.py\nM  unstaged_modified.py\n D deleted.py\n"
        ),
    )
    dirty, count = fc._is_dirty_tree(Path("."))
    assert dirty is True
    assert count == 3


def test_is_dirty_tree_all_untracked_many_files_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-11: many untracked files but zero tracked changes -> (False, 0)."""
    fc = _import_fix_command()
    lines = "".join(f"?? file{i}.py\n" for i in range(10))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _make_completed_process(lines),
    )
    dirty, count = fc._is_dirty_tree(Path("."))
    assert dirty is False
    assert count == 0
