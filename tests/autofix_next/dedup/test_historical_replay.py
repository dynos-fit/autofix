"""Historical-replay cascade test (AC #32).

Build a synthetic 2-commit fixture under tmp_path. Commit A contains
an unused-import finding at line 10. Commit B inserts 15 blank lines
above the import, moving it to line 25 -- the import text is
byte-identical. Run the cascade over both scans; both findings must
land in the SAME cluster.

Note: ``autofix_next.evidence.fingerprints.compute_finding_fingerprint``
takes only (rule_id, relpath, symbol_name, normalized_import) -- no
line numbers. So findings with identical rule/path/symbol/import but
different line numbers hash to the SAME finding_id, and tier 1 is
the expected collapse mechanism. No external git is required for
this test -- we synthesize CandidateFinding objects directly.
"""
from __future__ import annotations

from pathlib import Path

from autofix_next.dedup.cascade import DedupCascade
from autofix_next.dedup.cluster_store import ClusterStore
from autofix_next.evidence.fingerprints import compute_finding_fingerprint
from autofix_next.evidence.schema import CandidateFinding
from autofix_next.ranking.priority_scorer import PriorityScorer


def _build_finding(start_line: int, end_line: int, slice_text: str) -> CandidateFinding:
    rule_id = "unused-import.intra-file"
    path = "pkg/mod.py"
    symbol_name = "os"
    normalized_import = "os"
    fid = compute_finding_fingerprint(rule_id, path, symbol_name, normalized_import)
    return CandidateFinding(
        rule_id=rule_id,
        path=path,
        symbol_name=symbol_name,
        normalized_import=normalized_import,
        start_line=start_line,
        end_line=end_line,
        changed_slice=slice_text,
        finding_id=fid,
    )


class _EmptyGraph:
    symbol_count = 0
    all_symbols = frozenset()

    def callers_of(self, symbol_ids, max_depth):  # noqa: D401
        return frozenset()


def test_line_move_collapses_to_same_cluster(tmp_path: Path) -> None:
    # ---- scan of "commit A" ----------------------------------------
    store = ClusterStore()
    cascade = DedupCascade()
    scorer = PriorityScorer()

    finding_a = _build_finding(10, 10, "import os")
    score_a = scorer.score(finding_a, _EmptyGraph(), store)
    decision_a = cascade.classify(finding_a, score_a, store)
    assert decision_a.tier == 0, "first scan must open a new cluster"
    assert decision_a.is_new_cluster
    assert decision_a.novelty == 1.0

    # Persist between scans just like the pipeline does.
    store.save(tmp_path)

    # ---- scan of "commit B" (blank lines inserted; import moved) ---
    reloaded = ClusterStore.load(tmp_path)
    finding_b = _build_finding(25, 25, "import os")  # same text, different line
    # The line move must not change the finding_id -- AC #32 contract.
    # compute_finding_fingerprint takes (rule_id, relpath, symbol_name,
    # normalized_import) only; line numbers are NOT an input, so the
    # moved import hashes to the same finding_id and tier 1 collapses.
    assert finding_a.finding_id == finding_b.finding_id, (
        "compute_finding_fingerprint must be line-independent: "
        "the line move should not alter the fingerprint."
    )
    score_b = scorer.score(finding_b, _EmptyGraph(), reloaded)
    decision_b = cascade.classify(finding_b, score_b, reloaded)

    # Tier 1 collapse -- same fingerprint.
    assert decision_b.tier == 1, (
        f"expected tier 1 collapse on line-move; got tier={decision_b.tier}"
    )
    assert decision_b.novelty == 0.0
    assert decision_b.cluster_id == decision_a.cluster_id, (
        "line-moved finding must join the same cluster as the original"
    )


def test_first_scan_yields_novelty_one(tmp_path: Path) -> None:
    """AC #33 end-to-end: first scan against an empty store assigns
    novelty=1.0 to every finding."""
    store = ClusterStore()
    cascade = DedupCascade()
    scorer = PriorityScorer()

    # Build three distinct findings (different symbol_name /
    # normalized_import so their fingerprints differ).
    def _mk(sym: str) -> CandidateFinding:
        fid = compute_finding_fingerprint(
            "unused-import.intra-file", "pkg/mod.py", sym, sym
        )
        return CandidateFinding(
            rule_id="unused-import.intra-file",
            path="pkg/mod.py",
            symbol_name=sym,
            normalized_import=sym,
            start_line=1,
            end_line=1,
            changed_slice=f"import {sym}",
            finding_id=fid,
        )

    # Novelty is resolved from the scorer's view of the store *before*
    # each classify call. Because the scorer gates on
    # ``store.is_empty`` / ``find_by_fingerprint`` and NOT on simhash
    # neighbors, every finding in the first scan sees novelty=1.0 from
    # the scorer's perspective -- even if a subsequent cascade classify
    # collapses it into a previously-opened cluster via tier 2. AC #33
    # targets the scorer-emitted novelty signal, so we validate that.
    for f in [_mk("os"), _mk("sys"), _mk("json")]:
        score = scorer.score(f, _EmptyGraph(), store)
        assert score.breakdown["novelty"] == 1.0, (
            f"first-scan novelty must be 1.0 for {f.symbol_name}"
        )
        # classify still runs for side effects (cluster opens).
        cascade.classify(f, score, store)
    # At least one cluster must have been opened during the cold scan.
    assert store.cluster_count >= 1
    store.save(tmp_path)
