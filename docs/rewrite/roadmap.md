# Roadmap — sequenced migration tasks

- Source task: `task-20260417-001`
- Source report: `deep-research-report.md`
- Date: 2026-04-17

How to read a task: each entry below is a standalone brief for a single `/dynos-work:start` invocation. Operators do not need to read the full target-state design first; they can start from a task block, link out to the cited gap-analysis rows and target-architecture sections, and proceed. How to invoke: run `/dynos-work:start` with the task-slug (e.g. `events-ingress-vertical-slice-python`) as the task identifier; the discovery step re-reads `gap-analysis.md` and `target-architecture.md` to re-derive scope from the cited rows and sections.

The seven lock-path literals appear verbatim in every non-unlock task's Locked surfaces field. Do not plan work against those paths from within these tasks; if a task must change one of them, it is declared as a successor initiative with the unlock called out in its Goal statement. Twelve tasks are listed, falling cleanly in the 8–16 band from spec criterion 10.

---

### events-ingress-vertical-slice-python

**task-slug**: events-ingress-vertical-slice-python

**Goal**: Prototype a Python-only end-to-end vertical slice (git diff → Tree-sitter parse → one deterministic rule → evidence packet → LLM call → SARIF export) that proves the funnel before horizontal expansion.

**Phase**: Prototype

**acceptance criteria seeds**:

- A single `autofix-next scan` invocation on a Python repo with a seeded finding produces a SARIF file whose `partialFingerprints` match the scanned finding id.
- The LLM call is invoked exactly once per promoted candidate through `autofix.llm_backend.run_prompt` (locked) and never invoked on duplicates or suppressed paths.
- A replay of the same scan from `.autofix/events.jsonl` reproduces the same finding id and the same `prompt_prefix_hash`.
- End-to-end wiring covers git diff → Tree-sitter parse → deterministic rule → evidence packet → LLM → SARIF on a single language.

**Locked surfaces**:

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl`
- `benchmarks/agent_bench/**`

**Estimated size**: L

**Depends-on**: (none)

**Touches gap rows**: event ingress, change detector, incremental parser, deterministic analyzers (cheap), evidence-packet builder, LLM scheduler, SARIF export

**Touches target-architecture sections**: [Reference architecture](target-architecture.md#reference-architecture), [End-to-end scan sequence](target-architecture.md#end-to-end-scan-sequence), [Integration with locked surfaces](target-architecture.md#integration-with-locked-surfaces)

---

### invalidation-planner-core

**task-slug**: invalidation-planner-core

**Goal**: Build the incremental invalidation planner that maps a `ChangeSet` to the exact set of affected symbols, index shards, and graph edges.

**Phase**: Prototype

**acceptance criteria seeds**:

- Given a `ChangeSet` with N changed files, the planner emits an `Invalidation` plan that lists exactly the symbol IDs whose defining file is in the ChangeSet plus their transitive callers up to a bounded depth.
- A fresh-instance signal from the watcher downgrades the plan to a bounded full-sweep rather than a partial invalidation.
- Benchmarks show the planner runs in constant time per changed file plus linear time in transitive-callers depth; a pathological 10k-file change does not blow up invalidation runtime.

**Locked surfaces**:

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl`
- `benchmarks/agent_bench/**`

**Estimated size**: M

**Depends-on**: events-ingress-vertical-slice-python

**Touches gap rows**: invalidation planner, change detector, call/dependency graph

**Touches target-architecture sections**: [Module boundaries](target-architecture.md#module-boundaries), [End-to-end scan sequence](target-architecture.md#end-to-end-scan-sequence)

---

### symbol-index-scip-python

**task-slug**: symbol-index-scip-python

**Goal**: Land the SCIP-backed symbol/reference index for Python with an incremental-shard cache under `.autofix/state/index/`.

**Phase**: Alpha

**acceptance criteria seeds**:

- `scip-python` is invoked only on files named by the `Invalidation` plan, and the resulting shard is written to the locked `.autofix/state/**` layout through the existing `autofix/state.py` helpers.
- `SymbolRecord` lookups by stable symbol_id return results equivalent to the current `autofix/platform.build_import_graph` output on a benchmark repo.
- A cold rebuild on a 50kloc repo completes in under 120 seconds; an incremental update on a one-file change completes in under 3 seconds.

**Locked surfaces**:

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl`
- `benchmarks/agent_bench/**`

**Estimated size**: L

**Depends-on**: invalidation-planner-core

**Touches gap rows**: symbol/reference index, call/dependency graph, incremental parser

**Touches target-architecture sections**: [Module boundaries](target-architecture.md#module-boundaries), [Language registry](target-architecture.md#language-registry)

---

### language-registry-jsts-go

**task-slug**: language-registry-jsts-go

**Goal**: Extend the language registry with `JSTSAdapter` (scip-typescript) and `GoAdapter` (scip-go) so JS/TS and Go become first-class languages alongside Python.

**Phase**: Alpha

**acceptance criteria seeds**:

- `LanguageAdapter` protocol is implemented by both adapters; unit tests verify signature, kind, and scip-index emission on fixture repositories.
- An end-to-end scan on a mixed Python + TypeScript repository routes each file through the correct adapter without hard-coded language branches in core.
- `scip-go` invocation is per-module; a commit that only touches one module only re-indexes that module.

**Locked surfaces**:

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl`
- `benchmarks/agent_bench/**`

**Estimated size**: L

**Depends-on**: symbol-index-scip-python

**Touches gap rows**: incremental parser, symbol/reference index

**Touches target-architecture sections**: [Language registry](target-architecture.md#language-registry), [Module boundaries](target-architecture.md#module-boundaries)

---

### priority-scorer-and-dedup

**task-slug**: priority-scorer-and-dedup

**Goal**: Land the priority scorer and the three-tier dedup layer (exact SARIF fingerprint, SimHash, embedding similarity).

**Phase**: Alpha

**acceptance criteria seeds**:

- The priority scorer implements the research-report weighted formula and exposes every intermediate feature in the explanation record for replay.
- Exact dedup computes a SARIF-compatible `partialFingerprint` from normalized path + rule family + primary symbol ID + normalized AST hash.
- Near-dup clustering reduces alert spam on a historical replay: the same underlying issue across line-moved commits collapses into one cluster.

**Locked surfaces**:

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl`
- `benchmarks/agent_bench/**`

**Estimated size**: M

**Depends-on**: symbol-index-scip-python

**Touches gap rows**: priority scorer, dedup/cluster layer, suppression/policy engine, impact estimator, candidate findings store

**Touches target-architecture sections**: [Module boundaries](target-architecture.md#module-boundaries), [End-to-end scan sequence](target-architecture.md#end-to-end-scan-sequence)

---

### evidence-packet-builder-v1

**task-slug**: evidence-packet-builder-v1

**Goal**: Freeze the `EvidencePacket` v1 schema and land the builder so every downstream LLM call consumes the same contract.

**Phase**: Alpha

**acceptance criteria seeds**:

- The builder emits `EvidencePacket` with the six v1 fields (`rule_id`, `primary_symbol`, `changed_slice`, `supporting_symbols`, `prompt_prefix_hash`, `schema_version`) and refuses to produce a packet without them.
- `prompt_prefix_hash` is deterministic given the same packet inputs and matches the cache key used by `autofix.llm_backend.run_prompt`.
- A schema conformance test prevents accidental v1 field additions that would invalidate the prompt-prefix cache.

**Locked surfaces**:

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl`
- `benchmarks/agent_bench/**`

**Estimated size**: M

**Depends-on**: priority-scorer-and-dedup

**Touches gap rows**: evidence-packet builder, LLM scheduler, small-model triage, large-model report writer

**Touches target-architecture sections**: [Data schemas](target-architecture.md#data-schemas), [Integration with locked surfaces](target-architecture.md#integration-with-locked-surfaces)

---

### llm-scheduler-tiered

**task-slug**: llm-scheduler-tiered

**Goal**: Land the tiered LLM scheduler that gates, batches, caches, and budgets calls into `autofix.llm_backend.run_prompt` (locked).

**Phase**: Alpha

**acceptance criteria seeds**:

- The scheduler never calls the LLM on duplicates, suppressed paths, or generated/vendor code; test cases prove each negative case.
- Medium-priority findings go to the small-model triage; high-priority promoted findings go to the large-model report writer; the split is driven by the loaded policy thresholds from `.autofix/autofix-policy.json`.
- Prompt-prefix caching yields a measurable hit-rate on repeated scans; the cache key is the `prompt_prefix_hash` stamped by the evidence builder.

**Locked surfaces**:

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl`
- `benchmarks/agent_bench/**`

**Estimated size**: L

**Depends-on**: evidence-packet-builder-v1

**Touches gap rows**: LLM scheduler, small-model triage, large-model report writer, suppression/policy engine

**Touches target-architecture sections**: [Integration with locked surfaces](target-architecture.md#integration-with-locked-surfaces), [End-to-end scan sequence](target-architecture.md#end-to-end-scan-sequence)

---

### telemetry-replay-service

**task-slug**: telemetry-replay-service

**Goal**: Instrument the full pipeline with OpenTelemetry traces/logs/metrics and stand up a read-only replay service that reproduces past scans from `.autofix/events.jsonl` (telemetry and replay together).

**Phase**: Beta

**acceptance criteria seeds**:

- Every stage (ingress, change detection, invalidation, parse, index, analyze, rank, dedup, schedule, LLM) emits a span with a shared `event_id` correlation ID and commit SHA attributes.
- Replay of a historical `.autofix/events.jsonl` reproduces the same finding ids and the same `prompt_prefix_hash` on a fixed analyzer/policy version.
- The structured explanation record lets an operator answer: diff wrong, invalidation wrong, analyzer noisy, ranking bad, dedup collision, or LLM too permissive.

**Locked surfaces**:

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl`
- `benchmarks/agent_bench/**`

**Estimated size**: M

**Depends-on**: llm-scheduler-tiered

**Touches gap rows**: telemetry/traces, replay store, event ingress

**Touches target-architecture sections**: [Module boundaries](target-architecture.md#module-boundaries), [Integration with locked surfaces](target-architecture.md#integration-with-locked-surfaces)

---

### sarif-export-stable-fingerprints

**task-slug**: sarif-export-stable-fingerprints

**Goal**: Emit SARIF with stable `partialFingerprints` and path anchors so CI/code-host dashboards consume findings without duplicate alerts across line moves.

**Phase**: Beta

**acceptance criteria seeds**:

- `autofix-next export sarif --scan-id ...` produces a SARIF 2.1.0 file that validates against the OASIS schema.
- `partialFingerprints` are stable across a line-move-only commit; duplicate rate in a historical replay is materially lower than a line-number-only id scheme.
- A SARIF file from the new loop and the current scanner's finding set overlap on fingerprint for seeded fixtures, proving cross-compatibility. This task advances the SARIF export gap row in `gap-analysis.md`.

**Locked surfaces**:

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl`
- `benchmarks/agent_bench/**`

**Estimated size**: S

**Depends-on**: priority-scorer-and-dedup

**Touches gap rows**: SARIF export, dedup/cluster layer

**Touches target-architecture sections**: [Module boundaries](target-architecture.md#module-boundaries), [Clean-slate CLI surface](target-architecture.md#clean-slate-cli-surface)

---

### embedding-sidecar-precision-path

**task-slug**: embedding-sidecar-precision-path

**Goal**: Add the per-symbol/per-slice embedding sidecar and activate the semantic recall stage for promoted candidates.

**Phase**: Beta

**acceptance criteria seeds**:

- An HNSW-family ANN sidecar is maintained incrementally from the `SymbolRecord` stream; cold rebuild and incremental update latencies are measured.
- Semantic recall lifts near-dup clustering beyond what exact + SimHash achieve on a held-out fixture set.
- The sidecar is opt-in per repo via policy; disabling it does not break cheap-path scans.

**Locked surfaces**:

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl`
- `benchmarks/agent_bench/**`

**Estimated size**: L

**Depends-on**: symbol-index-scip-python

**Touches gap rows**: embedding index, dedup/cluster layer, lexical search index

**Touches target-architecture sections**: [Module boundaries](target-architecture.md#module-boundaries)

---

### state-migration-legacy-to-next

**task-slug**: state-migration-legacy-to-next

**Goal**: Migrate existing `.autofix/state/**` data into the new loop's consumption format without changing the locked on-disk schema.

**Phase**: Beta

**acceptance criteria seeds**:

- A one-shot migration reads the current `.autofix/state/current/findings.json` and produces an `autofix_next`-consumable in-memory view without rewriting the on-disk bytes.
- The new scheduler and dedup layer resolve legacy findings against the incoming `CandidateFinding` stream with zero drift on a fixture replay.
- A rollback path is documented: disabling `autofix-next` leaves the legacy `.autofix/state/**` readable by the current `autofix list` / `autofix policy` subcommands.

**Locked surfaces**:

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl`
- `benchmarks/agent_bench/**`

**Estimated size**: S

**Depends-on**: llm-scheduler-tiered

**Touches gap rows**: candidate findings store, suppression/policy engine, replay store

**Touches target-architecture sections**: [Integration with locked surfaces](target-architecture.md#integration-with-locked-surfaces), [Config compatibility](target-architecture.md#config-compatibility)

---

### clean-slate-cli-cutover

**task-slug**: clean-slate-cli-cutover

**Goal**: Ship the `autofix-next` CLI, migrate each deprecated subcommand per the mapping in target-architecture, and publish a retirement window for the legacy CLI.

**Phase**: Production

**acceptance criteria seeds**:

- Every subcommand listed in [Deprecated CLI surface](target-architecture.md#deprecated-cli-surface) has a clear replace/rename/remove action documented in release notes and exercised in integration tests.
- `autofix-next scan`, `autofix-next watch`, `autofix-next replay`, `autofix-next export sarif`, and `autofix-next policy --show` are all runnable on a reference repo without touching any locked surface.
- A retirement calendar is added to `docs/rewrite/` or release notes so operators with cron entries hitting `autofix scan --root` can plan their migration window.

**Locked surfaces**:

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl`
- `benchmarks/agent_bench/**`

**Estimated size**: M

**Depends-on**: telemetry-replay-service, sarif-export-stable-fingerprints, state-migration-legacy-to-next

**Touches gap rows**: event ingress, SARIF export, suppression/policy engine, telemetry/traces

**Touches target-architecture sections**: [Clean-slate CLI surface](target-architecture.md#clean-slate-cli-surface), [Deprecated CLI surface](target-architecture.md#deprecated-cli-surface), [Config compatibility](target-architecture.md#config-compatibility)

---

## Critical path

The minimum ordered sequence of tasks that must land before the scanner is production-capable (derived from a topological sort of `Depends-on`):

1. events-ingress-vertical-slice-python
2. invalidation-planner-core
3. symbol-index-scip-python
4. priority-scorer-and-dedup
5. evidence-packet-builder-v1
6. llm-scheduler-tiered
7. telemetry-replay-service
8. sarif-export-stable-fingerprints
9. state-migration-legacy-to-next
10. clean-slate-cli-cutover

## Parallelizable lanes

Tasks that can progress concurrently without interface conflict once their dependencies are satisfied:

- Lane A (precision depth after the symbol index lands): `language-registry-jsts-go`, `embedding-sidecar-precision-path`. Both depend only on `symbol-index-scip-python`; they touch different subpackages (`parsing`/`index`) and can be worked in parallel.
- Lane B (Beta deliverables after the LLM scheduler): `telemetry-replay-service`, `state-migration-legacy-to-next`. Both depend only on `llm-scheduler-tiered`; they touch disjoint subpackages (`telemetry` vs. migration scripts).
- Lane C (Beta exports after ranking/dedup): `sarif-export-stable-fingerprints`, `embedding-sidecar-precision-path`. Both depend on `priority-scorer-and-dedup` or `symbol-index-scip-python`; their outputs merge only at the `clean-slate-cli-cutover` task.
