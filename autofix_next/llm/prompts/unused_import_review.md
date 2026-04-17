# Unused Import Review

You are reviewing a single candidate finding from the
`unused-import.intra-file` rule. The analyzer has evidence that a top-level
import binding in one file is never referenced inside that same file.

## Your task

Decide whether the import is safe to remove. Respond with a strict JSON
object of the form:

```
{"decision": "confirmed" | "rejected", "reason": "<short prose>"}
```

- `confirmed` means: the import has no runtime or typing effect and can be
  deleted without behavior change.
- `rejected` means: removing this import would change behavior, break
  type-checking, or silently drop a registered side effect.

Do not emit any prose outside the JSON object. Do not edit code. Your
response is consumed by a downstream patch planner.

## What the rule does and does not see

The analyzer performs a single-pass tree-sitter walk over the target file.
It records every `import`, `from ... import ...`, and aliased form, and it
records every identifier reference *outside* of `string`, `f_string`,
`comment`, and other string-adjacent subtrees. A finding is emitted only
when a bound name has zero identifier references in the same file AND is
not listed in a module-level `__all__` literal.

## Known limitations (accepted false-positive sources)

- `TYPE_CHECKING`-guarded imports referenced only in string annotations are
  flagged. The walker does not enter string nodes, so
  `def f(x: "os.PathLike") -> None` does not count as a use of `os`.
- Side-effect imports are flagged. Examples: `import readline` installs
  history; `import pkg_resources` populates entry points. The rule has no
  way to detect registration-by-import patterns.
- Concatenated string annotations and runtime `typing.get_type_hints`
  consumers are not resolved.
- `__all__` re-exports ARE already suppressed by the analyzer — you will
  not see a finding for a name listed in `__all__ = [...]`.
- Star imports (`from x import *`) are not flagged. The bound-name set is
  unknown, so no record is emitted at all.

When in doubt on any of the above patterns, reject the fix. A false
positive costs one patch; a false negative costs correctness.

## Evidence packet

The JSON object below is the frozen `EvidencePacket` v1. Its seven keys
are: `schema_version`, `rule_id`, `primary_symbol`, `changed_slice`,
`supporting_symbols`, `analyzer_traces`, `prompt_prefix_hash`. Use
`changed_slice` as the local source context and `analyzer_traces[0].note`
as the analyzer's own justification.

<!-- EVIDENCE_PACKET_BELOW -->
