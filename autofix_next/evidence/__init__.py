"""autofix_next.evidence — EvidencePacket v1 schema, builder, and fingerprints.

The evidence subsystem produces a frozen-shape JSON packet per candidate
finding. The packet is the sole input to the LLM seam's prompt-prefix hash
and therefore must be byte-stable across re-invocations and Python versions.
"""
