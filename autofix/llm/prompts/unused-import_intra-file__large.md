# Unused Import Review (Large-Tier Deep Review)

You are reviewing a single candidate finding from the
`unused-import.intra-file` rule using the large-model tier. The
analyzer has evidence that a top-level import binding in one file is
never referenced inside that same file. Because you have been routed
here instead of the small-tier reviewer, the finding is either
large-slice, ambiguous, or carries elevated cross-file risk — apply
deeper reasoning before you answer.

## Your task

Decide whether the import is safe to remove. Respond with a strict
JSON object of the form:

```
{"decision": "confirmed" | "rejected", "reason": "<short prose>"}
```

- `confirmed` means: the import has no runtime or typing effect and
  can be deleted without behavior change.
- `rejected` means: removing this import would change behavior, break
  type-checking, or silently drop a registered side effect.

Do not emit any prose outside the JSON object. Do not edit code. Your
response is consumed by a downstream patch planner.

## What the rule does and does not see

The analyzer performs a single-pass tree-sitter walk over the target
file. It records every `import`, `from ... import ...`, and aliased
form, and it records every identifier reference *outside* of
`string`, `f_string`, `comment`, and other string-adjacent subtrees.
A finding is emitted only when a bound name has zero identifier
references in the same file AND is not listed in a module-level
`__all__` literal.

## Cross-reference context

Unlike the small-tier path, the large tier is invoked when the
changed slice is long, when the symbol name is generic
(`utils`, `helpers`, `models`), or when the module is a known entry
point / plugin host. Before answering, consider:

- Whether the module exposes the imported name to other packages via
  an implicit re-export (no `__all__` declared, so every top-level
  name is public under `from module import *`).
- Whether the imported binding is the canonical import location for
  the rest of the repo — deleting it here may break downstream
  imports even though the analyzer only sees one file.
- Whether the import participates in a circular-import workaround
  (imported at module top-level precisely to force an initialization
  order).

If any of these plausibly applies, prefer `rejected`. The large tier
exists to be conservative where the small tier would overfit.

## Indirect usage analysis

Walk the following checklist explicitly before deciding:

1. String-only references. The walker ignores `string`, `f_string`,
   and `comment` subtrees. Scan the `changed_slice` for the symbol
   inside quoted annotations (`"os.PathLike"`, `'datetime.datetime'`)
   and inside `typing.get_type_hints`-style consumers.
2. Side-effect imports. Modules such as `readline`, `pkg_resources`,
   `coverage`, and many `*.plugins.*` packages register behavior at
   import time. Absence of an identifier reference is expected and
   does NOT imply the import is unused.
3. `TYPE_CHECKING` blocks. Imports inside
   `if TYPE_CHECKING:` are referenced only by string annotations;
   confirm the symbol is unused across ALL annotations, not just
   runtime code.
4. `__all__` drift. The analyzer already suppresses findings for
   names in `__all__`, but a dynamic or conditionally-built
   `__all__` may defeat that check. If `__all__` is computed (not a
   module-level list literal), reject.
5. Re-export chains. A common pattern is
   `from .inner import Thing  # re-export`. If the changed slice or
   the analyzer note hints at a package `__init__.py`, reject unless
   the symbol is clearly dead.

## Decision rubric

- Confirm only when every item in the checklist above is clearly
  not triggered.
- Reject on any ambiguity. A false positive costs one patch; a false
  negative costs correctness.

## Evidence packet

The JSON object below is the frozen `EvidencePacket` v1. Its seven
keys are: `schema_version`, `rule_id`, `primary_symbol`,
`changed_slice`, `supporting_symbols`, `analyzer_traces`,
`prompt_prefix_hash`. Use `changed_slice` as the local source
context, `supporting_symbols` as the set of co-located names the
analyzer considered, and `analyzer_traces[0].note` as the analyzer's
own justification.

<!-- EVIDENCE_PACKET_BELOW -->
