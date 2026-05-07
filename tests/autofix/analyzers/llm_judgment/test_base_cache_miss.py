"""Test cache miss scenario (AC-10b).

Empty cache; monkeypatch Scheduler.invoke_judgment to return canned JSON;
call analyze; assert scheduler called once AND cache file written with
envelope shape (version: 1, key, model, commit_sha, created_at, findings).
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from tests.autofix.analyzers.llm_judgment.conftest import FakeJudgmentAnalyzer


def test_cache_miss_calls_scheduler_and_writes_cache(parse_result) -> None:
    """Cache miss calls scheduler once and writes cache file with correct envelope."""
    # Setup: canned LLM response
    llm_response = json.dumps([
        {
            "category": "unused-variable",
            "severity": "warning",
            "description": "Variable x is unused",
            "start_line": 1,
            "end_line": 1,
            "evidence": "x = 1",
        }
    ])

    # Execute: analyze with monkeypatched scheduler
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=llm_response,
    ) as mock_invoke:
        findings = list(FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None))

    # Assert: scheduler called exactly once
    assert mock_invoke.call_count == 1

    # Assert: findings generated
    assert len(findings) == 1
    assert findings[0].rule_id == "llm:fake:unused-variable"
    assert findings[0].path == "p.py"

    # Assert: cache file written
    cache_dir = parse_result.repo_root / ".autofix" / "cache" / "llm_judgment"
    assert cache_dir.exists()

    # Find the cache file (it should exist)
    cache_files = list(cache_dir.glob("*.json"))
    assert len(cache_files) == 1

    # Assert: cache file has correct envelope shape
    cache_path = cache_files[0]
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))

    assert "version" in envelope
    assert envelope["version"] == 1
    assert "key" in envelope
    assert "model" in envelope
    assert envelope["model"] == FakeJudgmentAnalyzer.MODEL
    assert "commit_sha" in envelope
    assert envelope["commit_sha"] == "_no_commit"
    assert "created_at" in envelope
    assert "findings" in envelope
    assert isinstance(envelope["findings"], list)
    assert len(envelope["findings"]) == 1
    assert envelope["findings"][0]["rule_id"] == "llm:fake:unused-variable"


def test_cache_miss_with_multiple_findings(parse_result) -> None:
    """Cache miss with multiple findings writes all to cache."""
    llm_response = json.dumps([
        {
            "category": "cat1",
            "severity": "high",
            "description": "Issue 1",
            "start_line": 1,
            "end_line": 1,
            "evidence": "x = 1",
        },
        {
            "category": "cat2",
            "severity": "low",
            "description": "Issue 2",
            "start_line": 2,
            "end_line": 3,
            "evidence": "y = 2",
        },
    ])

    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=llm_response,
    ):
        findings = list(FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None))

    assert len(findings) == 2

    # Verify cache
    cache_dir = parse_result.repo_root / ".autofix" / "cache" / "llm_judgment"
    cache_files = list(cache_dir.glob("*.json"))
    assert len(cache_files) == 1

    envelope = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert len(envelope["findings"]) == 2
    assert envelope["findings"][0]["rule_id"] == "llm:fake:cat1"
    assert envelope["findings"][1]["rule_id"] == "llm:fake:cat2"


def test_cache_miss_with_empty_response(parse_result) -> None:
    """Cache miss with empty LLM response writes empty findings to cache."""
    llm_response = json.dumps([])

    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=llm_response,
    ) as mock_invoke:
        findings = list(FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None))

    assert mock_invoke.call_count == 1
    assert findings == []

    # Cache should still be written
    cache_dir = parse_result.repo_root / ".autofix" / "cache" / "llm_judgment"
    cache_files = list(cache_dir.glob("*.json"))
    assert len(cache_files) == 1

    envelope = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert envelope["version"] == 1
    assert envelope["findings"] == []


def test_cache_envelope_has_deterministic_key(parse_result) -> None:
    """Cache envelope key field matches computed hash."""
    import hashlib

    llm_response = json.dumps([
        {
            "category": "test",
            "severity": "high",
            "description": "Test finding",
            "start_line": 1,
            "end_line": 1,
            "evidence": "x = 1",
        }
    ])

    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=llm_response,
    ):
        findings = list(FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None))

    # Compute expected cache key
    source_code = "x = 1\n"
    prompt = FakeJudgmentAnalyzer.prompt_template(source_code)
    cache_input = (prompt + "_no_commit" + FakeJudgmentAnalyzer.MODEL).encode(
        "utf-8"
    )
    expected_key = hashlib.sha256(cache_input).hexdigest()

    # Verify cache file name matches expected key
    cache_dir = parse_result.repo_root / ".autofix" / "cache" / "llm_judgment"
    cache_path = cache_dir / f"{expected_key}.json"
    assert cache_path.exists()

    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    assert envelope["key"] == expected_key
