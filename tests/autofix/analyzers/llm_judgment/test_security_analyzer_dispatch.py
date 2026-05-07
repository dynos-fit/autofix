"""End-to-end dispatch test for SecurityJudgmentAnalyzer (AC-12 of task-20260507-008).

Mirrors the dispatch shape used by ARCH-005's
`test_code_quality_analyzer_dispatch.py`:
- Use the autouse `_reset_state` and `parse_result` fixtures from conftest.
- Patch `autofix.llm.scheduler.Scheduler.invoke_judgment` with a canned JSON payload.
- Drive the analyzer via its public `analyze()` classmethod (the entry point that
  `run_scan` dispatches to per analyzer registration).

Verifies:
- A single CandidateFinding is produced with rule_id "llm:security:path-traversal".
- The finding's provenance starts with "llm:security:opus:" (the SARIF
  result.properties.source value carried through from analyze()) and carries a
  16-character hex cache-key suffix.
- The cache file at the deterministic SHA-256 cache key is written on disk at
  `<root>/.autofix/cache/llm_judgment/<sha256>.json`.
"""

from __future__ import annotations

import hashlib
import json
from unittest import mock

from autofix.analyzers.llm_judgment.security import SecurityJudgmentAnalyzer


def _canned_response() -> str:
    """Return the canned scheduler JSON payload used by every test below.

    Exactly one finding with `category == "path-traversal"` and the six
    required keys (category, severity, description, start_line, end_line,
    evidence).
    """
    return json.dumps(
        [
            {
                "category": "path-traversal",
                "severity": "critical",
                "description": "user input flows into open()",
                "start_line": 1,
                "end_line": 1,
                "evidence": "open(user_input)",
            }
        ]
    )


def test_dispatch_emits_namespaced_rule_id(parse_result) -> None:
    """A single finding emerges with rule_id 'llm:security:path-traversal'."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=_canned_response(),
    ) as mock_invoke:
        findings = list(
            SecurityJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
        )

    assert mock_invoke.call_count == 1, "Scheduler.invoke_judgment must be called exactly once"
    assert len(findings) == 1, f"expected exactly 1 finding, got {len(findings)}"
    assert findings[0].rule_id == "llm:security:path-traversal"


def test_dispatch_provenance_carries_model_namespace(parse_result) -> None:
    """The finding's provenance starts with 'llm:security:opus:' and has a 16-hex suffix."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=_canned_response(),
    ):
        findings = list(
            SecurityJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
        )

    assert len(findings) == 1
    provenance = findings[0].provenance
    assert provenance.startswith("llm:security:opus:"), (
        f"provenance must start with 'llm:security:opus:', got {provenance!r}"
    )
    # Defense-in-depth: provenance carries a 16-hex cache-key suffix after the prefix.
    suffix = provenance[len("llm:security:opus:"):]
    assert len(suffix) == 16, f"expected 16-char cache-key suffix, got {suffix!r}"
    int(suffix, 16)  # raises ValueError if the suffix is not hex


def test_dispatch_writes_cache_at_deterministic_key(parse_result) -> None:
    """The cache file at the deterministic SHA-256 key is written to disk."""
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=_canned_response(),
    ):
        findings = list(
            SecurityJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
        )

    assert len(findings) == 1

    # Recompute the deterministic cache key the same way _base.analyze does:
    #   sha256(prompt + commit_sha + MODEL)
    source_code = "x = 1\n"  # matches the parse_result fixture's file content
    prompt = SecurityJudgmentAnalyzer.prompt_template(source_code)
    cache_input = (prompt + "_no_commit" + SecurityJudgmentAnalyzer.MODEL).encode("utf-8")
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
    assert envelope["model"] == "opus"
    assert envelope["commit_sha"] == "_no_commit"
    assert len(envelope["findings"]) == 1
    assert envelope["findings"][0]["rule_id"] == "llm:security:path-traversal"
    assert envelope["findings"][0]["provenance"].startswith("llm:security:opus:")
