from pathlib import Path
import hashlib

from autofix.crawler import analyze_file_for_llm, build_crawl_plan, finalize_crawl_state, normalize_crawl_state
from autofix.llm_io import extract_json_array, validate_llm_issue, validate_llm_issues
from autofix.llm_io.prompting import build_review_chunks_for_file


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


def test_build_crawl_plan_marks_stale_review_state(tmp_path: Path) -> None:
    _write(tmp_path / "lib/src/example.dart", "class Example {}\n")
    state = {
        "files": {
            "lib/src/example.dart": {
                "path": "lib/src/example.dart",
                "last_llm_reviewed_at": "2026-03-01T00:00:00Z",
                "last_crawled_at": "2026-03-01T00:00:00Z",
                "last_crawl_hash": "currenthash",
                "content_hash": "currenthash",
                "last_result": "clean",
                "next_eligible_at": "2026-03-10T00:00:00Z",
            }
        }
    }

    next_state, plan = build_crawl_plan(tmp_path, state, [], max_files=1)

    entry = next(item for item in plan["frontier"] if item["path"] == "lib/src/example.dart")
    assert entry["stale_review"] is True
    assert any(reason["rule"] == "stale_review_ttl" for reason in entry["reasons"])
    assert any(reason["rule"] == "eligibility_window_open" for reason in entry["reasons"])
    assert next_state["files"]["lib/src/example.dart"]["review_ttl_days"] == 7


def test_neighbor_activity_invalidates_unchanged_file(tmp_path: Path) -> None:
    _write(tmp_path / "lib/src/a.dart", "class A {}\n")
    _write(tmp_path / "lib/src/b.dart", "class B {}\n")
    a_hash = hashlib.sha256("class A {}\n".encode()).hexdigest()[:16]
    state = {
        "files": {
            "lib/src/a.dart": {
                "path": "lib/src/a.dart",
                "last_llm_reviewed_at": "2026-04-07T00:00:00Z",
                "last_crawled_at": "2026-04-07T00:00:00Z",
                "last_crawl_hash": a_hash,
                "content_hash": a_hash,
                "changed_since_last_crawl": False,
                "last_result": "clean",
            },
            "lib/src/b.dart": {
                "path": "lib/src/b.dart",
                "last_llm_reviewed_at": "2026-04-08T00:00:00Z",
                "last_crawled_at": "2026-04-08T00:00:00Z",
                "changed_since_last_crawl": True,
            },
        }
    }

    _, plan = build_crawl_plan(tmp_path, state, [], max_files=2)
    frontier = plan["frontier"]
    entry = next(item for item in frontier if item["path"] == "lib/src/a.dart")
    assert any(reason["rule"] == "neighbor_changed" for reason in entry["reasons"])


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


def test_extract_json_array_handles_fenced_output() -> None:
    raw = """```json
[
  {"description": "bug", "file": "a.py", "line": 10, "severity": "high", "category_detail": "Logic bug", "confidence": 0.9}
]
```"""
    parsed = extract_json_array(raw)
    assert isinstance(parsed, list)
    assert parsed[0]["file"] == "a.py"


def test_validate_llm_issue_enforces_schema() -> None:
    allowed_files = {"src/example.py"}
    valid = validate_llm_issue(
        {
            "description": "real bug",
            "file": "src/example.py",
            "line": 12,
            "severity": "high",
            "category_detail": "Logic bug",
            "confidence": 0.91,
        },
        allowed_files=allowed_files,
    )
    invalid = validate_llm_issue(
        {
            "description": "wrong file",
            "file": "src/other.py",
            "line": "nope",
            "severity": "urgent",
            "category_detail": "",
            "confidence": 2,
        },
        allowed_files=allowed_files,
    )

    assert valid is not None
    assert valid["file"] == "src/example.py"
    assert invalid is None


def test_validate_llm_issues_filters_invalid_items() -> None:
    items = [
        {
            "description": "bug one",
            "file": "src/example.py",
            "line": 4,
            "severity": "medium",
            "category_detail": "Data integrity",
            "confidence": 0.7,
        },
        {
            "description": "bad",
            "file": "src/nope.py",
            "line": 0,
            "severity": "high",
            "category_detail": "Logic bug",
            "confidence": 0.8,
        },
    ]
    validated = validate_llm_issues(items, allowed_files={"src/example.py"})
    assert len(validated) == 1
    assert validated[0]["description"] == "bug one"


def test_build_review_chunks_for_large_file(tmp_path: Path) -> None:
    lines = "\n".join(f"line {i}" for i in range(1, 401))
    _write(tmp_path / "src/huge.py", lines)

    chunks = build_review_chunks_for_file(tmp_path, review_file="src/huge.py")

    assert len(chunks) >= 2
    assert chunks[0]["start_line"] == 1
    assert chunks[0]["end_line"] > chunks[0]["start_line"]
    assert chunks[-1]["end_line"] == 400


def test_build_crawl_plan_penalizes_giant_files(tmp_path: Path) -> None:
    huge_lines = "\n".join(f"line {i}" for i in range(1, 1201))
    _write(tmp_path / "lib/src/huge.dart", huge_lines)
    _write(
        tmp_path / "lib/src/risky.dart",
        "const apiKey = 'super-secret-token';\n"
        "Future<void> sync(String input) async {\n"
        "  final body = jsonDecode(input);\n"
        "  print(body);\n"
        "}\n",
    )

    _, plan = build_crawl_plan(tmp_path, {}, [], max_files=2)

    huge_entry = next(item for item in plan["frontier"] if item["path"] == "lib/src/huge.dart")
    risky_entry = next(item for item in plan["frontier"] if item["path"] == "lib/src/risky.dart")

    assert any(reason["rule"] == "large_file_penalty" for reason in huge_entry["reasons"])
    assert risky_entry["score"] > huge_entry["score"]
