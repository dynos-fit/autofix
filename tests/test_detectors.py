from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from autofix.detectors import detect_llm_review
from autofix.llm_backend import LLMBackendConfig


def _plan() -> tuple[dict, dict]:
    return (
        {"files": {}},
        {
            "selected_files": [{"path": "src/example.py", "score": 1.0}],
            "frontier": [{"path": "src/example.py", "score": 1.0}],
            "review_files": ["src/example.py"],
        },
    )


def test_detect_llm_review_does_not_persist_selection_state_when_claude_unavailable(tmp_path: Path) -> None:
    with (
        patch("autofix.detectors.load_scan_coverage", return_value={}),
        patch("autofix.detectors.load_findings", return_value=[]),
        patch("autofix.detectors.build_crawl_plan", return_value=_plan()),
        patch("autofix.detectors.shutil.which", return_value=None),
        patch("autofix.detectors.save_scan_coverage") as mock_save_scan_coverage,
        patch("autofix.detectors.write_scan_artifact") as mock_write_scan_artifact,
    ):
        findings = detect_llm_review(tmp_path, log=lambda _: None)

    assert findings == []
    mock_save_scan_coverage.assert_not_called()
    mock_write_scan_artifact.assert_not_called()


def test_detect_llm_review_does_not_persist_selection_state_when_openai_backend_is_unconfigured(tmp_path: Path) -> None:
    with (
        patch("autofix.detectors.load_scan_coverage", return_value={}),
        patch("autofix.detectors.load_findings", return_value=[]),
        patch("autofix.detectors.build_crawl_plan", return_value=_plan()),
        patch("autofix.detectors.save_scan_coverage") as mock_save_scan_coverage,
        patch("autofix.detectors.write_scan_artifact") as mock_write_scan_artifact,
    ):
        findings = detect_llm_review(
            tmp_path,
            log=lambda _: None,
            backend_config=LLMBackendConfig(backend="openai_compatible", base_url=""),
        )

    assert findings == []
    mock_save_scan_coverage.assert_not_called()
    mock_write_scan_artifact.assert_not_called()
