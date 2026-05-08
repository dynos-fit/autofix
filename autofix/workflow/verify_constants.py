"""Constants for autofix.workflow.verify (ARCH-011).

Side-effect-free module: every numeric and string literal the verify
module uses lives here. The verify module body MUST NOT inline these
values — the no-magic-numbers grep test (AC-21) treats inline literals
as a binding contract violation.

Mirrors the discipline established in :mod:`autofix.cli.run_constants`.
"""
from __future__ import annotations


DEFAULT_TIMEOUT_SECONDS: int = 300

# (marker_filename, runner_name, command_tuple) — first marker present at
# the project root wins. Order is the priority: Python first because
# autofix is itself a Python tool and the typical user is fixing Python
# code. Polyglot repos with multiple markers fall back to user config.
TEST_RUNNER_DETECTION_ORDER: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("pyproject.toml", "pytest", ("pytest", "-x")),
    ("setup.py", "pytest", ("pytest", "-x")),
    ("setup.cfg", "pytest", ("pytest", "-x")),
    ("package.json", "jest", ("npm", "test")),
    ("go.mod", "go-test", ("go", "test", "./...")),
)

CONFIG_PATH_RELATIVE: str = ".autofix/config.json"

VERIFY_LOG_FILENAME_TEMPLATE: str = "verify-{attempt}.log"

SUBPROCESS_EXIT_CODE_OK: int = 0


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "TEST_RUNNER_DETECTION_ORDER",
    "CONFIG_PATH_RELATIVE",
    "VERIFY_LOG_FILENAME_TEMPLATE",
    "SUBPROCESS_EXIT_CODE_OK",
]
