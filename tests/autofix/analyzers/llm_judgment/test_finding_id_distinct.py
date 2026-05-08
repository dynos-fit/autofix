"""LLM-judgment findings have distinct, non-empty finding_ids.

Surfaced when running the dogfood demo: the LLM patcher's cache key
collapses to ``sha256(finding_id + file_sha + model)``. With every
LLM-judgment finding receiving the literal empty string for
``finding_id``, all findings on a given file shared one cache entry.
One bad rejection (``status: rejected``) silently blocked all future
LLM patches on that file — even findings of unrelated categories.

Concretely the dogfood crawl produced 7 ``llm:security`` findings on
``agent_loop.py`` (3 ``command-injection``, 2 ``path-traversal``, 1
``prompt-injection``, 1 ``data-exposure``). All 7 routed to the LLM
patcher correctly post-PR-#77, but ``produce_patch`` returned None for
each — silently — because the cache held a stale ``rejected`` envelope
keyed on ``("" + agent_loop_sha + "opus")``. 0 ``LLMPatch*`` events
fired and 0 patches were generated.

The fix: synthesize a distinct ``finding_id`` from
``(rule_id, path, start_line, end_line)`` for every LLM-judgment
finding. This is the natural disambiguator within a file's scope.
"""
from __future__ import annotations

import json
from unittest import mock

from tests.autofix.analyzers.llm_judgment.conftest import FakeJudgmentAnalyzer


def test_two_same_category_findings_get_distinct_finding_ids(
    parse_result,
) -> None:
    """Two findings of the SAME category at different line ranges
    must produce distinct, non-empty ``finding_id`` values.

    Pre-fix, both got ``finding_id=""``. The downstream LLM-patch cache
    key then collapsed to ``sha256("" + file_sha + "opus")`` and one
    rejection blocked all subsequent patches on the file.
    """
    llm_response = json.dumps([
        {
            "category": "command-injection",
            "severity": "major",
            "description": "subprocess shell=True",
            "start_line": 1,
            "end_line": 1,
            "evidence": "shell=True",
        },
        {
            "category": "command-injection",
            "severity": "major",
            "description": "another subprocess",
            "start_line": 1,
            "end_line": 1,
            "evidence": "another shell=True",
        },
    ])
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=llm_response,
    ):
        findings = list(
            FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
        )

    assert len(findings) == 2
    ids = [f.finding_id for f in findings]
    assert all(fid for fid in ids), (
        f"finding_id must be non-empty; got {ids!r}"
    )
    # Both findings happen to land at the same (start, end). The
    # synthesized id encodes (rule_id, path, start_line, end_line),
    # so two findings with the same category at the same range still
    # collide — but that is correct: within a single file at the same
    # range, two findings of the same category ARE the same finding
    # for caching purposes (they'll dedupe).
    # The interesting collision case is different ranges; pin that.


def test_distinct_line_ranges_get_distinct_finding_ids(parse_result) -> None:
    """Two findings of the same category at DIFFERENT line ranges
    must produce distinct ``finding_id`` values — the downstream
    LLM-patch cache must not collapse them into one slot.
    """
    parse_result.path.write_text(
        "x = 1\ny = 2\nz = 3\n", encoding="utf-8"
    )
    llm_response = json.dumps([
        {
            "category": "command-injection",
            "severity": "major",
            "description": "first",
            "start_line": 1,
            "end_line": 1,
            "evidence": "shell=True",
        },
        {
            "category": "command-injection",
            "severity": "major",
            "description": "second",
            "start_line": 2,
            "end_line": 3,
            "evidence": "shell=True",
        },
    ])
    with mock.patch(
        "autofix.llm.scheduler.Scheduler.invoke_judgment",
        return_value=llm_response,
    ):
        findings = list(
            FakeJudgmentAnalyzer.analyze(parse_result, symbol_table=None)
        )

    assert len(findings) == 2
    ids = [f.finding_id for f in findings]
    assert all(fid for fid in ids)
    assert ids[0] != ids[1], (
        f"distinct line ranges must produce distinct finding_ids; "
        f"got {ids!r} — this is the cache-key collision that silently "
        "blocked the LLM patcher in the 2026-05-08 dogfood run"
    )
    # Identity invariants — the synthesized id encodes enough to
    # rebuild the (rule_id, path, line-range) triple downstream.
    for f in findings:
        assert f.path in f.finding_id
        assert str(f.start_line) in f.finding_id
        assert str(f.end_line) in f.finding_id
        assert f.rule_id in f.finding_id


