# Crawling improvements (task-20260508-002)

This page documents the second-wave changes to the crawl subsystem.
All improvements are gated behind opt-in flags in `.autofix/config.json`
under the new `crawler.*` namespace; with zero configuration the
crawler behaves exactly as it did before.

## What changed

### New modules

* `autofix/crawl/file_classifier.py` — pure-function `classify_file`
  returning a `FileClass` enum (12 values: `source`, `test`, `config`,
  `docs`, `entrypoint`, `generated`, `vendor`, `cache`, `build_output`,
  `lockfile`, `binary`, `unknown`). Plus `is_generated` (header-only,
  reads at most 8 KB) and `map_test_to_impl`.
* `autofix/crawl/crawl_observability.py` — `CycleStats` mutable
  dataclass and `emit_cycle_stats(stats, *, quiet, debug_crawl, file)`
  for per-cycle telemetry.

### Modified modules (additive only)

* `autofix/crawl/crawl_constants.py` — new constants:
  `MAX_BUNDLE_HOPS_ENTRYPOINT`, `MAX_RELEVANT_FILE_BYTES`,
  `OVERSIZE_FILE_PENALTY`, `LOW_VALUE_CLASS_PENALTY`,
  `ENTRYPOINT_BOOST`, `BUDGET_HIT_REASON_*`,
  `CLASS_EXPANSION_PRIORITY` dict.
* `autofix/crawl/score.py` — `relevance` accepts new optional kwargs
  (`file_class`, `file_size_bytes`, `scoring_flags`); without them the
  function is byte-identical to the prior implementation.
* `autofix/crawl/bundles.py` — `expand_bundle` accepts
  `class_aware_config`, `autofixignore` kwargs.
* `autofix/crawl/picker.py` — `pick_next_batch` accepts an
  `autofixignore` kwarg that filters seed candidates.
* `autofix/crawl/ledger.py` — `LedgerRow` extended with 4 optional v2
  fields: `scan_count_for_seed`, `imported_by_count_at_scan`,
  `bundle_size_bytes`, `budget_hit_reason`. Old-format JSONL rows
  still round-trip.
* `autofix/crawl/driver.py` — `_detect_working_tree_diff`,
  `_pick_impact_cone_batch`, `_should_use_impact_cone` (now reads
  `CrawlerFlags.impact_cone`); `debug_crawl: bool` plumbed through
  `run_crawl_once` and `run_crawl_continuously`.
* `autofix/crawl/config.py` — `CrawlerFlags` frozen dataclass and
  `read_crawler_flags(root)` reading the optional `crawler` namespace.
* `autofix/cli/main.py` — top-level `--debug-crawl` flag in the
  bare-crawl path.

### New repo-level feature

* `.autofixignore` — gitignore-style globs at the repo root. When
  present, the crawler excludes matched paths from seed candidates
  (in `pick_next_batch`) and from neighbor expansion (in
  `expand_bundle`). See `crawling-tuning.md` for the
  documented limitation: `autofixignore can only further-exclude`.

## Invariant tests

The default-off invariant is pinned by these tests — every one must
remain green for any change to ship:

* `tests/autofix/crawl/test_picker_determinism.py` — picker output for
  the mini-repo fixture is byte-identical with all flags off.
* `tests/autofix/crawl/test_score_supplemental_signals.py` — golden
  relevance values when no flags are passed.
* `tests/autofix/crawl/test_default_off_invariant.py` — pinned bundle
  fingerprints for the mini-repo; new constants are importable; class
  classifier is pure (no I/O).
* `tests/autofix/crawl/test_crawl_no_magic_numbers.py` — no inline
  literals in consumer modules.
* `tests/autofix/crawl/test_dispatcher_verify.py` — driver dispatch
  preserved.
* `tests/autofix/crawl/test_main_help_layered.py` — CLI help layered
  view (now includes `--debug-crawl` per AC 22).

## Opt-in keys (`.autofix/config.json`)

```json
{
  "crawler": {
    "scoring": {
      "entrypoint_boost": false,
      "low_value_class_penalty": false,
      "oversize_file_penalty": false
    },
    "expansion": {
      "class_aware": false
    },
    "modes": {
      "impact_cone": false
    }
  }
}
```

All keys default to `false`. Missing keys are equivalent to `false`.
A malformed config file falls back to the same all-`false` defaults.

See `crawling-tuning.md` for per-flag semantics.
