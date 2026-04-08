from pathlib import Path

from autofix.crawler import analyze_file_for_llm, build_crawl_plan, finalize_crawl_state, normalize_crawl_state


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_build_crawl_plan_prioritizes_unseen_and_changed_files(tmp_path: Path) -> None:
    _write(tmp_path / "src/new_hotspot.py", "def bug():\n    return 1\n")
    _write(
        tmp_path / "src/changed.py",
        "token = 'super-secret-token'\n"
        "def changed(data):\n"
        "    return eval(data)\n",
    )
    _write(tmp_path / "tests/test_changed.py", "def test_changed():\n    assert True\n")

    state = {
        "files": {
            "src/changed.py": {
                "path": "src/changed.py",
                "content_hash": "oldhash",
                "last_crawl_hash": "oldhash",
                "last_llm_reviewed_at": "2026-04-01T00:00:00Z",
                "last_result": "clean",
            }
        }
    }
    findings = [
        {
            "evidence": {"file": "src/changed.py"},
            "severity": "high",
            "status": "issue-opened",
            "found_at": "2026-04-07T00:00:00Z",
        }
    ]

    next_state, plan = build_crawl_plan(tmp_path, state, findings, max_files=2)

    assert plan["selected_files"]
    selected_paths = [item["path"] for item in plan["selected_files"]]
    assert "src/new_hotspot.py" in selected_paths
    unseen_entry = next(item for item in plan["frontier"] if item["path"] == "src/new_hotspot.py")
    assert any(reason["rule"] == "never_reviewed" for reason in unseen_entry["reasons"])
    changed_entry = next(item for item in plan["frontier"] if item["path"] == "src/changed.py")
    assert changed_entry["changed_since_last_crawl"] is True
    assert any(reason["rule"] == "content_changed" for reason in changed_entry["reasons"])
    assert changed_entry["detector_summary"]["signal_count"] >= 2
    assert any(reason["rule"] == "detector_signals" for reason in changed_entry["reasons"])
    assert next_state["repo"]["file_count"] == 3


def test_finalize_crawl_state_records_review_outcome(tmp_path: Path) -> None:
    _write(tmp_path / "src/example.py", "def example():\n    return 1\n")
    state = {
        "files": {
            "src/example.py": {
                "path": "src/example.py",
                "content_hash": "abc123",
                "selection_count": 1,
            }
        }
    }
    findings = [
        {
            "evidence": {"file": "src/example.py"},
            "severity": "critical",
            "status": "new",
            "found_at": "2026-04-08T00:00:00Z",
        }
    ]

    updated = finalize_crawl_state(state, ["src/example.py"], findings)
    file_state = updated["files"]["src/example.py"]

    assert file_state["last_result"] == "findings"
    assert file_state["last_finding_count"] == 1
    assert file_state["last_crawl_hash"] == "abc123"
    assert file_state["crawl_count"] == 1
    assert file_state["llm_review_count"] == 1
    assert file_state["changed_since_last_crawl"] is False
    assert updated["repo"]["last_reviewed_file_count"] == 1


def test_normalize_crawl_state_migrates_legacy_scan_fields() -> None:
    state = {
        "files": {
            "src/example.py": {
                "last_scanned_at": "2026-04-08T00:00:00Z",
                "last_result": "clean",
            }
        }
    }

    normalized = normalize_crawl_state(state)

    file_state = normalized["files"]["src/example.py"]
    assert file_state["last_llm_reviewed_at"] == "2026-04-08T00:00:00Z"
    assert file_state["last_crawled_at"] == "2026-04-08T00:00:00Z"


def test_analyze_file_for_llm_emits_detector_signals(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/risky.py",
        "API_KEY = 'super-secret-value'\n"
        "def run(cmd):\n"
        "    try:\n"
        "        eval(cmd)\n"
        "    except:\n"
        "        pass\n",
    )

    analysis = analyze_file_for_llm(tmp_path, "src/risky.py")

    assert analysis["summary"]["signal_count"] >= 3
    rules = {signal["rule"] for signal in analysis["signals"]}
    assert "secret_pattern" in rules
    assert "dynamic_execution" in rules
    assert "bare_except" in rules
