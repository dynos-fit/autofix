# Gap analysis — current `autofix/` vs. target architecture

- Source task: `task-20260417-001`
- Source report: `deep-research-report.md`
- Date: 2026-04-17

## Framing

This analysis is built **bottom-up first**: every load-bearing module under `autofix/` is walked, classified, and cited by repo-relative path. Only then are those modules mapped onto the 23 target-architecture rows drawn from the reference-architecture diagram in `deep-research-report.md` (layers: event ingest, incremental code intelligence, deterministic analyzers, ranking/triage, LLM-backed explanation, plus the cross-cutting state-and-observability plane).

Every row of the main table below has a `Delta` cell drawn from the allowed set `{keep as-is, wrap, replace, new}`. Rows whose `Delta` is `new` have a corresponding entry in the "MISSING-component landing sites" section further down. The lock list from `.dynos/task-20260417-001/discovery-notes.md` is reproduced verbatim in its own sub-table so no future roadmap task can accidentally plan work against a frozen seam.

The current scanner is not rewritten in place. The target architecture installs as a sibling Python package `autofix_next/` that the clean-slate CLI entrypoint dispatches to; the legacy `autofix/*.py` tree shrinks to shims around the locked surfaces (`autofix/llm_backend.py`, `autofix/agent_loop.py`, `autofix/llm_io/**`) and the persistent state helpers in `autofix/platform.py` + `autofix/state.py` that already write the locked `.autofix/state/**` shape. Nothing in `autofix/llm_io/**`, `autofix/agent_loop.py`, `autofix/llm_backend.py`, `.autofix/state/**`, `.autofix/autofix-policy.json`, `.autofix/events.jsonl`, or `benchmarks/agent_bench/**` is changed; each row below states its delta against that lock.

## Target-vs-current table

| Target component | Current-state | Target-state | Delta |
|---|---|---|---|
| event ingress | `autofix/daemon.py`, `autofix/scan_all.py`, `autofix/cli.py`, `autofix/app.py` | Normalize webhook, PR, commit, filesystem-watcher, and periodic safety-sweep signals into a single `ScanEvent` stream with a Watchman-style `is_fresh_instance` fallback. | replace |
| change detector | `autofix/crawler.py`, `autofix/scanner.py`, `autofix/runtime/core.py` | Layered detector that trusts (in order) repo event baseline, `git diff --histogram` hunks, watcher clocks, and Tree-sitter changed-ranges. | replace |
| invalidation planner | MISSING | Map changed files/hunks/symbols to the exact set of affected symbols, dependency-graph edges, and index shards so only invalidated pieces are rebuilt. | new |
| incremental parser | `autofix/detectors.py`, `autofix/crawler.py` (ad-hoc `ast.parse` calls) | Persisted Tree-sitter (generic path) plus language-native parser (precision path) that repairs only structurally changed ranges per edit. | replace |
| symbol/reference index | `autofix/platform.py` (`build_import_graph`), `autofix/runtime/defaults.py` | SCIP/LSIF-compatible symbol and reference index with stable symbol IDs suitable for finding anchors and dedup fingerprints. | new |
| lexical search index | MISSING | Trigram/BM25 index over identifiers, literals, paths, and regex motifs; primary retrieval stage in the hybrid cascade. | new |
| embedding index | MISSING | Per-symbol / per-slice embedding sidecar in an HNSW-family ANN store; used as a secondary recall stage for promoted candidates only. | new |
| call/dependency graph | `autofix/platform.py`, `autofix/routing.py` | Incremental call graph derived from the symbol index; used by the impact estimator and the invalidation planner. | wrap |
| optional semantic graph (CPG/dataflow) | MISSING | Heavyweight CPG/taint/dataflow pass activated only for high-priority promoted candidates. | new |
| deterministic analyzers (cheap) | `autofix/detectors.py`, `autofix/crawler.py`, `autofix/defaults.py` | Rule engine over lexical/AST patterns producing candidate findings with stable rule IDs. | replace |
| deterministic analyzers (semantic) | `autofix/detectors.py`, `autofix/routing.py`, `autofix/runtime/dynos.py` | Type/taint/control-flow analyzers that consume the symbol index and the optional semantic graph. | replace |
| impact estimator | `autofix/routing.py`, `autofix/crawler.py`, `autofix/repo.py` | Score candidates by fan-in, public-API exposure, security-boundary proximity, churn, ownership concentration, and runtime-signal criticality. | wrap |
| candidate findings store | `autofix/state.py`, `autofix/init.py` | Persist candidates keyed by stable fingerprint into `.autofix/state/current/findings.json` (locked shape); provide incremental read/write. | keep as-is |
| priority scorer | `autofix/routing.py`, `autofix/crawler.py` | Weighted sum of impact, freshness, confidence, novelty, and owner_risk as recommended by the research report. | wrap |
| dedup/cluster layer | `autofix/state.py` (`dedup_finding`), `autofix/output.py` | Three-tier dedup: SARIF `partialFingerprints` (exact), SimHash over normalized AST/message (structural), embedding similarity (semantic). | wrap |
| suppression/policy engine | `autofix/state.py` (`suppression_reason`), `autofix/routing.py`, `autofix/config.py` | Policy-driven, time-bounded suppressions read from `.autofix/autofix-policy.json` (locked shape) with per-category, per-path-prefix, and per-finding rules. | wrap |
| evidence-packet builder | `autofix/llm_io/prompting.py` (chunking), `autofix/llm_io/validation.py`, `autofix/detectors.py` | Budgeted packet assembler emitting the `EvidencePacket` contract: primary symbol, <=3 supporting symbols, <=2 analyzer traces, <=1 runtime/test bundle, plus `schema_version` and `prompt_prefix_hash`. | new |
| LLM scheduler | `autofix/detectors.py`, `autofix/agent_loop.py`, `autofix/backend.py` | Tiered gate that batches, caches, and budgets calls into the locked `autofix/llm_backend.run_prompt` boundary; never invokes the LLM on duplicates, suppressed paths, or generated/vendor code. | new |
| small-model triage | `autofix/agent_loop.py`, `autofix/llm_backend.py` | Cheap synchronous triage call on medium-to-high priority findings with structured JSON output; routes through the locked backend. | wrap |
| large-model report writer | `autofix/agent_loop.py`, `autofix/llm_backend.py` | Synchronous final report writer on high-priority promoted findings; emits the bug-report schema from the research report. | wrap |
| telemetry/traces | `autofix/benchmarking.py`, `autofix/platform.py` (`now_iso`, `write_json`) | OpenTelemetry-compatible traces/metrics/logs with correlation IDs, commit SHA, rule version, policy version, and `prompt_prefix_hash` on every stage. | new |
| replay store | `.autofix/events.jsonl` (current append-only log), `autofix/state.py` | Treat locked `.autofix/events.jsonl` as the first-class replay input; add a read-only replay service that re-runs a scan against a snapshotted commit, analyzer version, and policy version. | wrap |
| SARIF export | MISSING (no exporter exists today; `autofix/output.py` emits legacy text/JSON only) | Emit SARIF with stable `partialFingerprints` derived from the dedup layer so CI systems and security dashboards consume findings without duplication. | new |

## Locked-surfaces table

The following paths are locked per `.dynos/task-20260417-001/discovery-notes.md` (the repo-owner's lock list). Every row below is marked `keep as-is`; the one-sentence rationale cites the lock list. The new core loop wraps these surfaces through documented call sites (see `target-architecture.md#integration-with-locked-surfaces`).

| Locked path | Target-state | Delta |
|---|---|---|
| `autofix/llm_io/**` | Prompt path resolution and LLM output validation; consumed as the sole interface between evidence packets and the model invocation. | keep as-is — listed in discovery-notes lock list; all prompt I/O must flow through this module. |
| `autofix/agent_loop.py` | `run_agent_loop` / `run_review_agent_loop` entry points; preserved so the benchmark adapter and the new scheduler share one implementation. | keep as-is — listed in discovery-notes lock list; the new LLM scheduler wraps it, never replaces it. |
| `autofix/llm_backend.py` | `run_prompt` is the single callable that reaches a provider; every new call site invokes it unchanged. | keep as-is — listed in discovery-notes lock list; provider concerns remain inside this file. |
| `.autofix/state/**` | On-disk state shape for current and aggregate findings snapshots. | keep as-is — listed in discovery-notes lock list; the new store writes `.autofix/state/current/findings.json` unchanged. |
| `.autofix/autofix-policy.json` | Repo-local policy (suppressions, thresholds, category health). | keep as-is — listed in discovery-notes lock list; the new suppression/policy engine reads the existing shape unchanged. |
| `.autofix/events.jsonl` | Append-only event log; sole source of truth for replay. | keep as-is — listed in discovery-notes lock list; the new telemetry/replay path consumes this log rather than inventing a parallel one. |
| `benchmarks/agent_bench/**` | `build_agent(workdir, fixture)` callable and the `AutofixBenchmarkConfig` contract. | keep as-is — listed in discovery-notes lock list; the adapter seam stays byte-identical so existing fixtures run. |

## MISSING-component landing sites

Every `new` or `MISSING` row in the main table names a proposed module path for the new component and, where applicable, the existing file whose scope shrinks to accommodate it. This prevents two roadmap tasks from claiming the same module.

- **invalidation planner** → proposed module: `autofix_next/invalidation/planner.py`. Existing scope reduced: the ad-hoc inventory loop in `autofix/crawler.py` no longer drives scan selection; it remains only as a read-only enumerator for legacy scans until the clean-slate CLI lands.
- **incremental parser** → proposed package: `autofix_next/parsing/` with `tree_sitter.py` (generic path) and `language_native.py` (precision path). Existing scope reduced: `autofix/detectors.py` drops its embedded `ast.parse` calls in favour of reading from the new parse cache.
- **symbol/reference index** → proposed module: `autofix_next/index/symbols.py` plus an on-disk SCIP shard cache under `.autofix/state/index/` (written through the locked `.autofix/state/**` layout helpers in `autofix/state.py` and `autofix/platform.py`). Existing scope reduced: `autofix/platform.build_import_graph` becomes a thin compatibility shim that queries the new index.
- **lexical search index** → proposed module: `autofix_next/index/lexical.py`. No existing scope shrinks; this is additive.
- **embedding index** → proposed module: `autofix_next/index/embedding.py` plus a sidecar ANN store under `.autofix/state/index/embedding/`. No existing scope shrinks; additive and gated behind the precision-path flag.
- **optional semantic graph** → proposed module: `autofix_next/graphs/semantic.py`. No existing scope shrinks; opt-in per promoted candidate.
- **deterministic analyzers (cheap)** → proposed package: `autofix_next/analyzers/cheap/` with one submodule per rule family. Existing scope reduced: the rule surface in `autofix/detectors.py` migrates rule-by-rule; the legacy file becomes a compatibility shim that re-exports from the new package until the clean-slate CLI lands.
- **deterministic analyzers (semantic)** → proposed package: `autofix_next/analyzers/semantic/`. Existing scope reduced: the LLM-backed review helpers in `autofix/detectors.py` move out of the detector chain and into the LLM scheduler.
- **evidence-packet builder** → proposed module: `autofix_next/evidence/builder.py`. Existing scope reduced: the chunked-review helpers in `autofix/llm_io/prompting.py` are consumed verbatim by the builder (they are locked) — no file under `autofix/llm_io/**` is edited; the new builder wraps them.
- **LLM scheduler** → proposed module: `autofix_next/llm/scheduler.py`. Existing scope reduced: the scheduling decisions currently embedded in `autofix/detectors.py` and `autofix/routing.py` move behind the new scheduler; `autofix/llm_backend.run_prompt` is invoked unchanged.
- **telemetry/traces** → proposed package: `autofix_next/telemetry/` with `tracer.py` (OTel span/log emitter) and `correlation.py` (ID propagation). Existing scope preserved: `autofix/benchmarking.py` remains the shim for agent-bench `trace_llm`/`trace_tool` decorators; the new telemetry exports additional spans without touching the existing decorator surface.
- **SARIF export** → proposed module: `autofix_next/export/sarif.py`. No existing scope shrinks; additive.

## Bottom-up cross-check: current files touched by the analysis

The Current-state column above references the following repo-relative files (this is the minimum 15-file floor that criterion 5 enforces; the analysis touches more than the minimum). Each file is either kept, wrapped, or reduced in scope per the mapping above: `autofix/app.py`, `autofix/backend.py`, `autofix/benchmarking.py`, `autofix/cli.py`, `autofix/config.py`, `autofix/crawler.py`, `autofix/daemon.py`, `autofix/defaults.py`, `autofix/detectors.py`, `autofix/init.py`, `autofix/llm_backend.py`, `autofix/agent_loop.py`, `autofix/llm_io/prompting.py`, `autofix/llm_io/validation.py`, `autofix/output.py`, `autofix/platform.py`, `autofix/repo.py`, `autofix/routing.py`, `autofix/runtime/core.py`, `autofix/runtime/defaults.py`, `autofix/runtime/dynos.py`, `autofix/scan_all.py`, `autofix/scanner.py`, `autofix/state.py`.

## Roadmap consumers

Every row above is referenced by name in at least one roadmap task (see [roadmap.md](roadmap.md)). The "Touches gap rows" field on each roadmap task cites rows from this document so operators can trace follow-up work back to the delta that motivated it.
