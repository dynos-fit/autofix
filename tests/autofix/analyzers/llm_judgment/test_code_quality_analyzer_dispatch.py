"""End-to-end dispatch test for CodeQualityJudgmentAnalyzer (AC-12 of task-20260507-004).

Mirrors the dispatch shape used elsewhere in this directory:
- Use the autouse `_reset_state` and `parse_result` fixtures from conftest.
- Patch `autofix.llm.scheduler.Scheduler.invoke_judgment` with a canned JSON payload.
- Drive the analyzer via its public `analyze()` classmethod (the entry point that
  `run_scan` dispatches to per analyzer registration).

Verifies:
- A single CandidateFinding is produced with rule_id "llm:code-quality:error-handling-gap".
- The finding's provenance starts with "llm:code-quality:sonnet:" (the SARIF
  result.properties.source value carried through from analyze()).
- The cache file at the deterministic SHA-256 cache key is written on disk.
"""

from __future__ import annotations

import hashlib
import json
from unittest import mock

from autofix.analyzers.llm_judgment.code_quality import CodeQualityJudgmentAnalyzer


def _canned_response() -> str:
    """Return the canned scheduler JSON payload used by every test below."""
    return json.dumps(
        [
            {
                "category": "error-handling-gap",
                "severity": "major",
                "description": "test",
                "start_line": 1,
                "end_line": 1,
                "evidence": "x",
            }
        ]
    )


def test_dispatch_emits_namespaced_rule_id(parse_result) -> None:
    """A single finding emerges with rule_id 'llm:code-quality:error-handling-gap'."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=_canned_response(),
    ) as mock_invoke:
        findings = list(
            CodeQualityJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
        )

    assert mock_invoke.call_count == 1, "Scheduler.invoke_judgment must be called exactly once"
    assert len(findings) == 1, f"expected exactly 1 finding, got {len(findings)}"
    assert findings[0].rule_id == "llm:code-quality:error-handling-gap"


def test_dispatch_provenance_carries_model_namespace(parse_result) -> None:
    """The finding's provenance (SARIF result.properties.source) starts with 'llm:code-quality:sonnet:'."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=_canned_response(),
    ):
        findings = list(
            CodeQualityJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
        )

    assert len(findings) == 1
    provenance = findings[0].provenance
    assert provenance.startswith("llm:code-quality:sonnet:"), (
        f"provenance must start with 'llm:code-quality:sonnet:', got {provenance!r}"
    )
    # Defense-in-depth: provenance carries a 16-hex cache-key suffix after the prefix.
    suffix = provenance[len("llm:code-quality:sonnet:"):]
    assert len(suffix) == 16, f"expected 16-char cache-key suffix, got {suffix!r}"
    int(suffix, 16)  # raises ValueError if the suffix is not hex


def test_dispatch_writes_cache_at_deterministic_key(parse_result) -> None:
    """The cache file at the deterministic SHA-256 key is written to disk."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=_canned_response(),
    ):
        findings = list(
            CodeQualityJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
        )

    assert len(findings) == 1

    # Recompute the deterministic cache key the same way _base.analyze does:
    #   sha256(prompt + commit_sha + MODEL)
    source_code = "x = 1\n"  # matches the parse_result fixture's file content
    prompt = CodeQualityJudgmentAnalyzer.prompt_template(source_code)
    cache_input = (prompt + "_no_commit" + CodeQualityJudgmentAnalyzer.MODEL).encode("utf-8")
    expected_key = hashlib.sha256(cache_input).hexdigest()

    cache_path = (
        parse_result.repo_root
        / ".autofix"
        / "cache"
        / "llm_judgment"
        / f"{expected_key}.json"
    )
    assert cache_path.exists(), f"cache file not written at expected path: {cache_path}"

    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    assert envelope["version"] == 1
    assert envelope["key"] == expected_key
    assert envelope["model"] == "sonnet"
    assert envelope["commit_sha"] == "_no_commit"
    assert len(envelope["findings"]) == 1
    assert envelope["findings"][0]["rule_id"] == "llm:code-quality:error-handling-gap"
    assert envelope["findings"][0]["provenance"].startswith("llm:code-quality:sonnet:")
