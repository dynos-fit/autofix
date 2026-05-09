# Crawling tuning

This page documents the optional flags and the `.autofixignore` file
used to tune the autofix crawler. All flags default to **off**; with
zero configuration the crawler behaves exactly as it did before
task-20260508-002.

## `.autofixignore`

A repo-root file with gitignore-style globs. When present, the
crawler excludes matched paths from:

* seed candidates (in `pick_next_batch`)
* neighbor expansion (in `expand_bundle`)

Seeds of an already-built bundle are **never** excluded by the file —
only candidate seeds and neighbors. This mirrors the existing
"hub saturation" rule.

### Documented limitation

`autofixignore can only further-exclude` paths from the crawl.

The crawler's seed source is `git ls-files`, which already honors
`.gitignore`. A `.autofixignore` pattern that attempts to *un-exclude*
something `.gitignore` excluded will have no observable effect — the
path was never in the candidate set in the first place. To be
explicit: `.autofixignore` stacks on top of `.gitignore`; it cannot
override it.

If you need a `.gitignore`-excluded path to be scanned, the right
move is to remove the `.gitignore` rule (or edit it to exclude the
specific files you actually want excluded) — not to add a
counter-rule to `.autofixignore`.

### Malformed patterns

Each pattern is validated independently. A malformed pattern is
skipped with a single stderr warning; the rest of the file still
parses.

If the `pathspec` runtime dependency is missing the loader logs a
warning and returns a no-op instance — `.autofixignore` support is
silently disabled rather than crashing the cycle.

## Class-aware expansion

Set `crawler.expansion.class_aware = true` in `.autofix/config.json`
to enable. When on, `expand_bundle` consults the file classifier:

* **`test` seeds** prioritize the mirror impl file (resolved via
  `map_test_to_impl`) first.
* **`config` seeds** are hard-capped at 1 hop and only `source`
  neighbors are kept.
* **`entrypoint` seeds** use `MAX_BUNDLE_HOPS_ENTRYPOINT` hops
  instead of 1.
* **junk-sink classes** (`generated`, `vendor`, `cache`,
  `build_output`, `lockfile`, `binary`) are dropped at every hop
  before the bytes-budget check.

Filter ordering at every hop is:

1. hub saturation
2. junk-sink class filter
3. autofixignore filter
4. config-seed non-source filter
5. bytes-budget cap

A hub-saturated junk-sink candidate is recorded as
*dropped-by-hub*, not *dropped-by-class* — saturation fires first.

### Known limitation: monorepo test layouts

`map_test_to_impl` auto-detects only the standard `tests/<pkg>/...`
mirror layout. Monorepos that put `tests/` under each package root
(e.g. `services/api/tests/...`) will silently fall back to no-op
mapping — no impl file will be prioritized. This is a documented
limitation, not a bug; the spec scopes auto-detection to the standard
layout.

## Flags reference

| Key | Default | Effect |
| --- | --- | --- |
| `crawler.scoring.entrypoint_boost` | `false` | Adds `ENTRYPOINT_BOOST` to relevance for `entrypoint`-class files |
| `crawler.scoring.low_value_class_penalty` | `false` | Multiplies relevance by `LOW_VALUE_CLASS_PENALTY` for low-value classes |
| `crawler.scoring.oversize_file_penalty` | `false` | Multiplies relevance by `OVERSIZE_FILE_PENALTY` when a file exceeds `MAX_RELEVANT_FILE_BYTES` |
| `crawler.expansion.class_aware` | `false` | Enables class-aware bundle expansion (see above) |
| `crawler.modes.impact_cone` | `false` | Seed from working-tree diff instead of relevance scorer |

The default-off invariant is pinned by
`tests/autofix/crawl/test_picker_determinism.py` and the golden-file
test in `tests/autofix/crawl/test_score_supplemental_signals.py` —
both must remain green.
