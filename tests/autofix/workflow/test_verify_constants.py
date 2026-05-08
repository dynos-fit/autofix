"""Constants-module shape tests for ARCH-011 (AC-1)."""
from __future__ import annotations

from autofix.workflow import verify_constants


def test_default_timeout_seconds() -> None:
    assert verify_constants.DEFAULT_TIMEOUT_SECONDS == 300
    assert isinstance(verify_constants.DEFAULT_TIMEOUT_SECONDS, int)


def test_subprocess_exit_code_ok() -> None:
    assert verify_constants.SUBPROCESS_EXIT_CODE_OK == 0


def test_config_path_relative() -> None:
    assert verify_constants.CONFIG_PATH_RELATIVE == ".autofix/config.json"


def test_log_filename_template() -> None:
    template = verify_constants.VERIFY_LOG_FILENAME_TEMPLATE
    assert template.format(attempt=3) == "verify-3.log"


def test_detection_order_shape() -> None:
    order = verify_constants.TEST_RUNNER_DETECTION_ORDER
    assert isinstance(order, tuple)
    assert len(order) == 5
    markers = [entry[0] for entry in order]
    assert "pyproject.toml" in markers
    assert "package.json" in markers
    assert "go.mod" in markers
    # Python (pyproject.toml) comes BEFORE JS (package.json) and Go (go.mod)
    assert markers.index("pyproject.toml") < markers.index("package.json")
    assert markers.index("package.json") < markers.index("go.mod")


def test_all_exports() -> None:
    expected = {
        "DEFAULT_TIMEOUT_SECONDS",
        "TEST_RUNNER_DETECTION_ORDER",
        "CONFIG_PATH_RELATIVE",
        "VERIFY_LOG_FILENAME_TEMPLATE",
        "SUBPROCESS_EXIT_CODE_OK",
    }
    assert set(verify_constants.__all__) == expected
