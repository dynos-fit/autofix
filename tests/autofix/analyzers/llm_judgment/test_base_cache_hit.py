"""Test cache hit scenario (AC-10a).

Pre-populate the cache file at the deterministic key, call analyze(),
assert Scheduler.invoke_judgment was NOT called AND findings match cached.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock


from tests.autofix.analyzers.llm_judgment.conftest import FakeJudgmentAnalyzer


def test_cache_hit_returns_cached_findings(parse_result: Path) -> None:
    """Pre-populate cache; analyze should return cached findings without calling scheduler."""
    # Setup: create cache directory and file with canned findings
    cache_dir = parse_result.repo_root / ".autofix" / "cache" / "llm_judgment"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Generate prompt and cache key to match what analyze() will compute
    source_code = "x = 1\n"
    prompt = FakeJudgmentAnalyzer.prompt_template(source_code)
    import hashlib

    cache_input = (prompt + "_no_commit" + FakeJudgmentAnalyzer.MODEL).encode(
        "utf-8"
    )
    cache_key = hashlib.sha256(cache_input).hexdigest()

    # Canned findings
    cached_findings = [
        {
            "rule_id": "llm:fake:test-category",
            "path": "p.py",
            "symbol_name": "test-symbol",
            "normalized_import": "",
            "start_line": 1,
            "end_line": 1,
            "changed_slice": "test finding",
            "finding_id": "",
            "provenance": "llm:fake:fake-model:abc123",
        }
    ]

    # Write cache envelope
    envelope = {
        "version": 1,
        "key": cache_key,
        "model": FakeJudgmentAnalyzer.MODEL,
        "commit_sha": "_no_commit",
        "created_at": "2026-04-17T00:00:00Z",
        "findings": cached_findings,
    }
    cache_path = cache_dir / f"{cache_key}.json"
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    # Execute: analyze should use cache
    with mock.patch("autofix.llm.scheduler.Scheduler.invoke_judgment") as mock_invoke:
        findings = list(FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None))

    # Assert: scheduler NOT called
    assert not mock_invoke.called

    # Assert: findings match cached
    assert len(findings) == 1
    assert findings[0].rule_id == "llm:fake:test-category"
    assert findings[0].path == "p.py"
    assert findings[0].symbol_name == "test-symbol"
    assert findings[0].start_line == 1
    assert findings[0].end_line == 1


def test_cache_hit_with_multiple_findings(parse_result: Path) -> None:
    """Cache with multiple findings returns all of them."""
    cache_dir = parse_result.repo_root / ".autofix" / "cache" / "llm_judgment"
    cache_dir.mkdir(parents=True, exist_ok=True)

    source_code = "x = 1\n"
    prompt = FakeJudgmentAnalyzer.prompt_template(source_code)
    import hashlib

    cache_input = (prompt + "_no_commit" + FakeJudgmentAnalyzer.MODEL).encode(
        "utf-8"
    )
    cache_key = hashlib.sha256(cache_input).hexdigest()

    # Multiple findings
    cached_findings = [
        {
            "rule_id": "llm:fake:cat1",
            "path": "p.py",
            "symbol_name": "sym1",
            "normalized_import": "",
            "start_line": 1,
            "end_line": 1,
            "changed_slice": "finding 1",
            "finding_id": "",
            "provenance": "llm:fake:fake-model:abc123",
        },
        {
            "rule_id": "llm:fake:cat2",
            "path": "p.py",
            "symbol_name": "sym2",
            "normalized_import": "",
            "start_line": 2,
            "end_line": 3,
            "changed_slice": "finding 2",
            "finding_id": "",
            "provenance": "llm:fake:fake-model:def456",
        },
    ]

    envelope = {
        "version": 1,
        "key": cache_key,
        "model": FakeJudgmentAnalyzer.MODEL,
        "commit_sha": "_no_commit",
        "created_at": "2026-04-17T00:00:00Z",
        "findings": cached_findings,
    }
    cache_path = cache_dir / f"{cache_key}.json"
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    with mock.patch("autofix.llm.scheduler.Scheduler.invoke_judgment"):
        findings = list(FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None))

    assert len(findings) == 2
    assert findings[0].rule_id == "llm:fake:cat1"
    assert findings[1].rule_id == "llm:fake:cat2"


def test_cache_hit_with_empty_findings(parse_result: Path) -> None:
    """Cache with empty findings list returns empty iterator."""
    cache_dir = parse_result.repo_root / ".autofix" / "cache" / "llm_judgment"
    cache_dir.mkdir(parents=True, exist_ok=True)

    source_code = "x = 1\n"
    prompt = FakeJudgmentAnalyzer.prompt_template(source_code)
    import hashlib

    cache_input = (prompt + "_no_commit" + FakeJudgmentAnalyzer.MODEL).encode(
        "utf-8"
    )
    cache_key = hashlib.sha256(cache_input).hexdigest()

    envelope = {
        "version": 1,
        "key": cache_key,
        "model": FakeJudgmentAnalyzer.MODEL,
        "commit_sha": "_no_commit",
        "created_at": "2026-04-17T00:00:00Z",
        "findings": [],
    }
    cache_path = cache_dir / f"{cache_key}.json"
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    with mock.patch("autofix.llm.scheduler.Scheduler.invoke_judgment") as mock_invoke:
        findings = list(FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None))

    assert not mock_invoke.called
    assert findings == []
