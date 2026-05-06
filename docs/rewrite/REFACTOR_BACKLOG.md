# Refactor backlog — large modules in autofix_next

Follow-up work items for modules whose line count has drifted past the
"one concern per module" guideline established in
`docs/rewrite/target-architecture.md`. Each section records the current
LOC (as measured on the `main` branch at task-20260417-010), the
distinct concerns that are currently bundled under a single module, and
two to three candidate decomposition strategies. **No code changes are
in scope for this document** — it exists to queue the work for later
tasks.

The backlog is ordered by descending LOC. When a module is split, the
new files inherit the parent's tests; test files should be split in
lockstep so each test module tracks one concern.

---

## 1. `autofix_next/invalidation/call_graph.py` — 1092 LOC

### Distinct concerns currently bundled

1. **AST parsing / import extraction.** Walks Python source trees to
   pull ``import`` and ``from X import Y`` statements. Fully stateless;
   the only external dependency is the stdlib ``ast`` module.
2. **Module-path resolution.** Maps dotted import names (``pkg.sub.mod``)
   to filesystem paths inside the scanned repository. Requires repo-
   root context and package-layout heuristics (``__init__.py``
   discovery, namespace-package handling, ``src/`` layout detection).
3. **Call-graph construction.** Builds the actual
   ``Mapping[symbol, set[symbol]]`` of edges. Cross-cutting — consumes
   both the parsed AST and the resolved module paths.
4. **Cache reconciliation.** Persists and loads the call graph from
   the per-scan-root cache directory; handles cache-miss / partial-
   invalidation rebuilds and interacts with the atomic-write helper.
5. **Telemetry emission.** Emits ``InvalidationComputed`` events on
   every rebuild; measures cold-start vs warm-reuse paths.
6. **Impacted-set computation.** Given a changeset, performs the
   transitive-closure walk to produce the "must-rescan" set. Consumes
   the fully-materialised call graph but is otherwise orthogonal to the
   build path.

### Candidate decomposition strategies

- **(A) Split by concern boundary.** Three new modules:
  `invalidation/ast_imports.py` (concerns 1+2),
  `invalidation/call_graph_builder.py` (concerns 3+4+5),
  `invalidation/impact_walker.py` (concern 6). Each is ~300-400 LOC
  with a focused test suite. Trade-off: requires defining an
  interchange dataclass for "resolved imports" that the builder
  consumes.
- **(B) Split by lifecycle phase.** `build.py` (cold-start rebuild),
  `incremental.py` (partial invalidation), `query.py` (impacted-set
  walks). Trade-off: duplicates the AST-parsing layer across build
  and incremental phases unless a shared `_ast_imports.py` helper is
  also extracted.
- **(C) Extract the cache/telemetry layer only.** Keep parsing, path
  resolution, and graph construction in a slimmed `call_graph.py`
  (~700 LOC); move cache I/O and event emission to
  `invalidation/call_graph_cache.py` (~200 LOC) and
  `invalidation/call_graph_telemetry.py` (~150 LOC). Smallest
  blast radius; leaves the core algorithm in one module for easier
  review but does not reduce the largest concern.

Recommended first pass: **(A)** — it attacks the highest-LOC concerns
first and establishes a clear data-flow seam.

---

## 2. `autofix_next/llm/scheduler.py` — 1003 LOC

(Measured at 971 LOC on the file-system snapshot taken while preparing
this backlog; the spec's 1003 figure reflects an earlier HEAD. Treat
both numbers as "roughly 1000".)

### Distinct concerns currently bundled

1. **Decision gating.** Evaluates the ten ``LLMCallGated`` decisions
   (``promoted``, ``skipped_suppressed``, ``skipped_duplicate_hash``,
   ``skipped_generated``, ``skipped_below_threshold``,
   ``skipped_budget_exceeded``, ``promoted_cache_hit``,
   ``promoted_default_tier``, ``cache_store_failed``,
   ``promoted_failed``) against the incoming packet/scoring pair.
2. **Cache put/get.** Reads and writes the suggestion cache under
   `~/.cache/autofix-next/llm/<hash>.json` using the atomic-write
   helper; enforces the LRU eviction policy.
3. **Budget accounting.** Tracks per-scan token-in / token-out /
   dollar-amount spend against the policy's budget ceiling; emits
   ``skipped_budget_exceeded`` when the budget is blown.
4. **Concurrency fan-out.** Owns the ``ThreadPoolExecutor`` that
   fans out ``schedule_many`` calls; propagates the OTel context to
   worker threads (tested by ``test_tracer_contextvar_propagation``).
5. **Policy loading.** Hydrates the scheduler from a YAML/JSON policy
   file, validates the shape, and applies defaults.
6. **Event emission.** Every decision path emits exactly one
   ``LLMCallGated`` event with the matching ``decision`` field.
7. **Replay gate.** When running in replay mode
   (``_REPLAY_EVENTS_SINK`` active), routes events to the in-memory
   sink instead of writing them to disk.

### Candidate decomposition strategies

- **(A) Strategy-per-decision.** Extract each of the ten decisions
  into its own function in `llm/gates/<decision>.py`; keep
  `scheduler.py` as a thin dispatcher. Trade-off: high number of tiny
  files; the dispatcher still has to enforce mutual-exclusion ordering.
- **(B) Horizontal split by responsibility.** Four new modules:
  `llm/cache.py` (concern 2), `llm/budget.py` (concern 3),
  `llm/policy.py` (concern 5), `llm/scheduler.py` (concerns 1+4+6+7,
  ~500 LOC). Clear seams; the cache and budget modules become
  stateful collaborators injected into the scheduler constructor.
- **(C) Pipeline decomposition.** Treat the scheduler as a pipeline
  of stages: `suppression_gate`, `dedup_gate`, `generated_gate`,
  `threshold_gate`, `budget_gate`, `cache_gate`, `invoke_stage`. Each
  stage takes `(packet, score, context)` and returns either a
  decision-with-reason or the next stage's input. Trade-off: needs
  a well-defined `SchedulerContext` dataclass; higher up-front design
  cost but easier to reason about the order of gates.

Recommended first pass: **(B)** — it pulls the two obvious extractable
units (cache + budget) out without committing to the more invasive
pipeline design.

---

## 3. `autofix_next/indexing/scip_index.py` — 927 LOC

(Measured at 893 LOC on the file-system snapshot; spec's 927 figure is
from an earlier revision. Treat both numbers as "roughly 900".)

### Distinct concerns currently bundled

1. **Protobuf decoding.** Parses the binary ``.scip`` shard format
   into the in-memory record dataclasses.
2. **Symbol-record normalisation.** Maps SCIP's string-keyed symbol
   records into the autofix-native
   ``(symbol_id, file, kind, range)`` tuples consumed by the rest of
   the pipeline.
3. **Shard merging.** Concatenates multiple shards (one per module-
   root in the Go adapter's case) into a single in-memory index;
   deduplicates overlapping symbol records by ``(file, symbol_id)``.
4. **Persistence.** Writes the merged index out as a deterministic
   JSON blob via `atomic_write_json` (task-010 AC 14-18).
5. **Cache reconciliation.** Decides when an existing on-disk index
   can be reused vs re-built from shards; interacts with
   ``.autofix-next/state/index/``.
6. **Concurrency.** Acquires the shard-dir flock to coordinate with
   concurrent ``GoAdapter.scip_index`` / ``JSTSAdapter.scip_index``
   writers.

### Candidate decomposition strategies

- **(A) Split codec from store.** `indexing/scip_codec.py` (concerns
  1+2+3) + `indexing/scip_store.py` (concerns 4+5+6). Clean seam —
  the codec is pure functions; the store is the stateful component
  that needs locking and cache bookkeeping.
- **(B) Split by shard-lifetime phase.** `indexing/scip_shard.py`
  (decode + normalise one shard, concerns 1+2), `indexing/scip_merge.py`
  (multi-shard combine, concern 3), `indexing/scip_persist.py`
  (persistence + cache, concerns 4+5+6). Most granular but leaves
  three small modules that may re-converge later.
- **(C) Strip persistence only.** Move concerns 4+5+6 to
  `indexing/scip_index_store.py` (~250 LOC) and keep decode / merge /
  normalise in `scip_index.py` (~650 LOC). Smallest-blast-radius
  change; leaves the read-side algorithm intact for reviewers.

Recommended first pass: **(A)** — cleanest separation of pure-function
codec work from stateful cache/lock concerns; aligns with the
`atomic_write_json` boundary introduced in task-010 seg-3.

---

## Notes

- None of the proposals above should be attempted inside a single task.
  Each one warrants a dedicated `docs/rewrite/` spec with an
  acceptance-criteria list, a migration plan for callers, and a paired
  test-suite split.
- Line counts will drift as further drift-cleanup tasks land. Re-measure
  before quoting a figure in a follow-up task spec.
- The three modules above are the only files currently over 900 LOC in
  `autofix_next/`. If a future module crosses that threshold, add a new
  section to this backlog rather than deferring to a case-by-case task.
