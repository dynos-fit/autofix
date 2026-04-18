"""Schema conformance tests for task-20260417-007.

Covers AC #7, #8, #9, #10 from .dynos/task-20260417-007/spec.md.

Asserts the v1 field name+order contract for EvidencePacket and
CandidateFinding, plus SCHEMA_VERSION pinning. Set + order, no type
assertion (Q3 decision).

Imports of the module-level constants ``EVIDENCE_V1_FIELD_ORDER`` and
``CANDIDATE_FINDING_V1_FIELD_ORDER`` are deferred inside each test body
so that pytest --collect-only stays clean on the TDD-gate checkout
where those names do not yet exist in ``autofix_next.evidence.schema``.
The tests fail at test-runtime (not collection-time) until the executor
in the execute phase lands the constants.
"""
from __future__ import annotations


def test_evidence_packet_v1_field_order_frozen() -> None:
    """AC #8: EvidencePacket fields iterated in declaration order match
    EVIDENCE_V1_FIELD_ORDER exactly (names + order). Any add/remove/
    rename/reorder trips this test and forces a paired SCHEMA_VERSION bump.
    """
    from dataclasses import fields

    from autofix_next.evidence.schema import (
        EVIDENCE_V1_FIELD_ORDER,
        EvidencePacket,
    )

    actual = tuple(f.name for f in fields(EvidencePacket))
    assert actual == EVIDENCE_V1_FIELD_ORDER, (
        f"EvidencePacket field list drifted from v1 contract.\n"
        f"Expected: {EVIDENCE_V1_FIELD_ORDER}\n"
        f"Got:      {actual}"
    )


def test_candidate_finding_v1_field_order_frozen() -> None:
    """AC #9: CandidateFinding fields iterated in declaration order match
    CANDIDATE_FINDING_V1_FIELD_ORDER exactly. analyzer_confidence (kw_only,
    task-005) is expected last. Any future addition trips the test and
    forces a paired SCHEMA_VERSION bump.
    """
    from dataclasses import fields

    from autofix_next.evidence.schema import (
        CANDIDATE_FINDING_V1_FIELD_ORDER,
        CandidateFinding,
    )

    actual = tuple(f.name for f in fields(CandidateFinding))
    assert actual == CANDIDATE_FINDING_V1_FIELD_ORDER, (
        f"CandidateFinding field list drifted from v1 contract.\n"
        f"Expected: {CANDIDATE_FINDING_V1_FIELD_ORDER}\n"
        f"Got:      {actual}"
    )


def test_schema_version_pinned() -> None:
    """AC #10: SCHEMA_VERSION is pinned at "evidence_v1". Any bump requires
    an explicit, paired change here.
    """
    from autofix_next.evidence.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == "evidence_v1"
