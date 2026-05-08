"""No-magic-numbers grep test for verify.py (ARCH-011 AC-21).

Treats inline literals in `autofix/workflow/verify.py` as a binding
contract violation. The constants module is the only place these
values may live.
"""
from __future__ import annotations

import re
from pathlib import Path

VERIFY_PATH = (
    Path(__file__).resolve().parents[3]
    / "autofix"
    / "workflow"
    / "verify.py"
)


def _read_source() -> str:
    return VERIFY_PATH.read_text(encoding="utf-8")


def test_default_timeout_value_not_inlined() -> None:
    src = _read_source()
    # Bare 300 anywhere in the body would mean the default-timeout
    # constant has been duplicated.
    assert not re.search(r"\b300\b", src), (
        "verify.py must reference DEFAULT_TIMEOUT_SECONDS, not the literal 300"
    )


def test_test_command_lists_not_inlined() -> None:
    src = _read_source()
    forbidden = [
        '"pytest", "-x"',
        "'pytest', '-x'",
        '"npm", "test"',
        "'npm', 'test'",
        '"go", "test"',
        "'go', 'test'",
    ]
    for needle in forbidden:
        assert needle not in src, (
            f"verify.py must not inline test command list {needle!r}"
        )


def test_marker_filenames_not_inlined() -> None:
    src = _read_source()
    forbidden_markers = ["pyproject.toml", "package.json", "go.mod"]
    for marker in forbidden_markers:
        assert marker not in src, (
            f"verify.py must not inline marker filename {marker!r}"
        )


def test_config_path_not_inlined() -> None:
    src = _read_source()
    assert ".autofix/config.json" not in src, (
        "verify.py must reference CONFIG_PATH_RELATIVE, not the literal path"
    )


def test_log_filename_template_not_inlined() -> None:
    src = _read_source()
    assert "verify-{" not in src, (
        "verify.py must reference VERIFY_LOG_FILENAME_TEMPLATE, not "
        "inline the format string"
    )
    assert ".log" not in src or "VERIFY_LOG_FILENAME_TEMPLATE" in src, (
        "verify.py references .log without using the template constant"
    )


def test_constants_module_imported() -> None:
    """Defensive: verify.py must import from verify_constants."""
    src = _read_source()
    assert "from autofix.workflow.verify_constants import" in src
