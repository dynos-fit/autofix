"""No-magic-numbers grep contract for autofix/cli/_watch_loop.py (ARCH-014, AC-18)."""
from __future__ import annotations

import re
from pathlib import Path

WATCH_LOOP_PATH = (
    Path(__file__).resolve().parents[3]
    / "autofix"
    / "cli"
    / "_watch_loop.py"
)


def _read_source() -> str:
    return WATCH_LOOP_PATH.read_text(encoding="utf-8")


def test_no_inline_business_numbers() -> None:
    """AC-18: _watch_loop.py must not inline business-relevant numeric
    constants (poll timeouts, safety-sweep defaults, retry counts,
    cycle limits). Control-flow integers (``0``, ``1``) used for
    initialization and one-shot counting are NOT magic numbers in the
    project's sense — they are direct expressions of language
    semantics, not configurable thresholds.

    The forbidden literals below would each represent a business
    decision frozen into code; if any appears, it should be moved to
    a constants module instead. This mirrors the targeted-literal
    style of ``test_run_command_no_magic_numbers``.
    """
    src = _read_source()
    src_no_docstrings = re.sub(
        r'"""[\s\S]*?"""', "", src, flags=re.MULTILINE
    )

    # Poll-timeout default lives in WatcherSession (watch_command), NOT here.
    assert "timeout_seconds=2" not in src_no_docstrings
    assert "timeout=2.0" not in src_no_docstrings

    # Safety-sweep default thresholds (60, 3600 seconds) belong in the
    # parser in watch_command, never inlined here.
    assert re.search(r"\b3600\b", src_no_docstrings) is None
    assert re.search(r"\b60\b", src_no_docstrings) is None

    # Cycle-budget magic numbers (max retries, max cycles per session)
    # belong on `args` (set by argparse default constants from
    # run_constants), never inlined here.
    assert re.search(r"max_retries\s*=\s*\d+", src_no_docstrings) is None
    assert re.search(r"max_cycles\s*=\s*\d+", src_no_docstrings) is None


def test_no_direct_pywatchman_import() -> None:
    """AC-30: helper module must preserve the deferred-import discipline.

    _watch_loop.py is invoked from both watch_command and run_command;
    it must NOT import pywatchman at module load. The session object
    is duck-typed and constructed by the caller.
    """
    src = _read_source()
    # Strip docstrings to avoid false positives on prose mentioning pywatchman.
    src_no_docstrings = re.sub(
        r'"""[\s\S]*?"""', "", src, flags=re.MULTILINE
    )
    assert "import pywatchman" not in src_no_docstrings
    assert "from pywatchman" not in src_no_docstrings
