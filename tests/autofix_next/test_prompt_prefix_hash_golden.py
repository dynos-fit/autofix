"""Golden-hash regression test for task-20260417-007.

Covers AC #11, #12. Pins compute_prompt_prefix_hash output against a
canonical sample packet. Any perturbation of the hash algorithm or the
canonical_json_bytes convention (sort_keys, separators, ensure_ascii,
UTF-8) trips this test.

The GOLDEN literal is resolved at implementation time by the executor
running:

    python -c "from autofix_next.evidence.fingerprints import \\
        compute_prompt_prefix_hash; print(compute_prompt_prefix_hash({ ... }))"

against the _SAMPLE_PACKET dict below, then pasting the 64-char
lowercase hex result into the GOLDEN constant. Today the literal is
"<to-be-filled-at-implementation>" so this test FAILS deliberately.
"""
from __future__ import annotations


_SAMPLE_PACKET: dict = {
    "schema_version": "evidence_v1",
    "rule_id": "unused-import.intra-file",
    "primary_symbol": "pkg/mod.py::foo",
    "changed_slice": "import os\n",
    "supporting_symbols": ["pkg/mod.py::bar"],
    "analyzer_traces": [
        {
            "rule_id": "unused-import.intra-file",
            "primary_symbol": "pkg/mod.py::foo",
            "changed_slice": "import os\n",
            "supporting_symbols": ["pkg/mod.py::bar"],
            "analyzer_note": "bound name os has zero identifier references",
            "prompt_prefix_hash": "",
        }
    ],
}


# Resolved at implementation time — one-shot compute against _SAMPLE_PACKET.
GOLDEN: str = "<to-be-filled-at-implementation>"


def test_prompt_prefix_hash_golden_value() -> None:
    """AC #12: The prompt_prefix_hash of the canonical sample packet is
    pinned. Drift here means either compute_prompt_prefix_hash changed,
    canonical_json_bytes changed, or the sample dict was perturbed — each
    is a v1 contract break and REQUIRES SCHEMA_VERSION bump + paired
    GOLDEN update.
    """
    from autofix_next.evidence.fingerprints import compute_prompt_prefix_hash

    actual = compute_prompt_prefix_hash(_SAMPLE_PACKET)
    assert actual == GOLDEN, (
        f"prompt_prefix_hash drifted from golden value.\n"
        f"Expected GOLDEN = {GOLDEN}\n"
        f"Got            = {actual}\n"
        f"If this is intentional, bump SCHEMA_VERSION in schema.py AND "
        f"update GOLDEN here with the new hash."
    )
