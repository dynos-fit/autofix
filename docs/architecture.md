# Architecture

How `autofix` works internally. Read this if you're contributing or
trying to understand why a scan produced what it produced.

## The 5-layer funnel

A scan flows through five layers. Each one narrows the candidate set
before the next:

```
git diff
    │
    ▼
Layer 1: Event ingress
    │  Turn `git diff` into a ChangeSet (paths + watcher confidence).
    │  Watchman emits a fresh-instance signal on cold start; the
    │  detector falls back to a bounded full sweep.
    ▼
Layer 2: Incremental code intelligence
    │  Tree-sitter parses each changed file.
    │  SCIP index records symbols + references (per-language).
    │  Embedding sidecar (optional) stores per-symbol vectors.
    │  Call graph tracks who calls what.
    ▼
Layer 3: Deterministic analyzers
    │  Cheap: lint/regex/heuristics (today: unused-import.intra-file).
    │  Semantic: types/CFG/taint (roadmap).
    │  Impact estimator: how many callers are affected.
    ▼
Layer 4: Ranking + triage
    │  Priority scorer assigns 0-1 priority per finding.
    │  Three-tier dedup:
    │    - Tier 1: exact fingerprint (sha256 of normalized path +
    │              rule + symbol + AST hash)
    │    - Tier 2: SimHash (rough syntactic similarity, Hamming ≤ 3)
    │    - Tier 3: embedding cosine (semantic similarity ≥ 0.85)
    │  Suppression engine drops findings matching policy globs.
    │  Evidence packet builder freezes the LLM input.
    ▼
Layer 5: LLM explanation
    │  Tiered scheduler routes:
    │    - Medium-priority → small-model triage
    │    - High-priority → large-model report writer
    │  Prompt-prefix cache reuses identical prompt prefixes.
    │  Budget gate stops at the configured per-scan token cap.
    ▼
SARIF + .autofix/events.jsonl
```

## Determinism

Every layer is deterministic given:

1. The same git commit SHA
2. The same analyzer/policy versions
3. The same event log

Replay (`autofix replay`) re-runs layers 1-4 and confirms the finding
ids match. Layer 5 isn't re-run — LLM responses aren't deterministic.

## Locked surfaces

Two on-disk paths are read-only after this loop initializes:

- `.autofix/state/current/findings.json` — legacy snapshot, consumed
  by the migration helper (read-only)
- `.autofix/autofix-policy.json` — your policy (read-only by autofix;
  edit it manually)

Internally, `autofix.agent_loop`, `autofix.llm_backend`, and
`autofix.llm_io/` are the LLM seam. Tests enforce that no other
module references `run_prompt` or `claude` directly.

## SCIP indexing

The new loop persists a SCIP-format symbol/reference index under
`.autofix/state/index/`. Per-shard cache keys are:

```
sha256(<file content> + <analyzer version> + <git blob sha>)
```

This means a re-scan of an unchanged file is a cache hit; no
subprocess invocation. For Go and TypeScript, the index is built by
the upstream `scip-go` / `scip-typescript` binaries (see README
"External binaries" section). For Python, the index is built directly
from Tree-sitter parse trees.

## Event log

`.autofix/events.jsonl` is an append-only log of every observable
event in a scan. One line per event, JSON. Event types include:

| Event | When |
|---|---|
| `ScanStarted` | Beginning of a scan |
| `ChangeSetComputed` | After git diff resolves to paths |
| `InvalidationComputed` | After the planner picks affected files |
| `FileParsed` | Per file, post-Tree-sitter |
| `CandidateFindingProduced` | Per analyzer hit |
| `PriorityScored` | After the scorer runs |
| `FindingDeduped` | After cascade decision |
| `EvidencePacketBuilt` | Right before the scheduler |
| `LLMCallGated` | Per scheduler decision |
| `EvidencePacketCached` / `EvidencePacketCacheMiss` | Prompt cache hit/miss |
| `SARIFEmitted` | After the SARIF writer |
| `ScanCompleted` | End of scan |
| `ScanExplanation` | Six-counter summary for operator debugging |

Replay reconstructs everything from these rows.

## SARIF output

`autofix` emits SARIF 2.1.0 with two `partialFingerprints`:

- `autofixNext/v1` — internal finding_id; stable across line moves
- `sarif-v1` — SARIF-consumer-friendly fingerprint; stable across
  symbol-name and file-path normalizations

Both are 64-char lowercase hex sha256 strings.

## Why this design

Three forces shaped the architecture:

1. **Diff-scoped scans are cheap.** Most scans should look at <10 files.
   The funnel narrows aggressively so the LLM only sees what matters.
2. **The LLM is the expensive layer.** Everything else is bounded; the
   scheduler/cache exists to keep token spend predictable.
3. **CI must be reproducible.** Determinism + replay let you debug a
   "why did this fire?" question post-mortem, without re-running the
   scan from scratch.

## Adding a new analyzer

1. Drop a new module under `autofix/analyzers/cheap/` (or
   `autofix/analyzers/semantic/` for index-aware analyzers).
2. Implement `analyze(file, parse_tree, scip_index) -> Iterable[CandidateFinding]`.
3. Register it in `autofix/funnel/pipeline.py`.
4. Add a fixture-based test under `tests/autofix/analyzers/`.

The funnel handles ranking, dedup, evidence-packet construction, and
LLM scheduling — your analyzer only emits raw candidate findings.

## Adding a new language

1. Add an adapter under `autofix/languages/` implementing the
   `LanguageAdapter` protocol (see `autofix/languages/__init__.py`).
2. The adapter is responsible for: producing SCIP-format index data
   (via Tree-sitter or an upstream binary), and registering the file
   extensions it handles.
3. The adapter auto-registers via `autofix.languages.adapter_emission`.

`autofix/languages/python.py` is the simplest reference. `go.py` and
`jsts.py` show the upstream-binary pattern.

## Above the funnel: the run loop

The 5-layer funnel produces findings. The **run loop** (shipped via
the architecture upgrade ARCH-001..015) consumes those findings and
optionally repairs them:

```
                ┌────────── (loop driver) ──────────┐
                │   autofix run [--apply] [--watch]   │
                └────────────────────────────────────┘
                                │
                                ▼
        ┌──────────────────── workflow loop ─────────────────────┐
        │  SCANNING → TRIAGING → PLANNING → APPLYING →            │
        │  VERIFYING → DONE | RETRY | HUMAN_REVIEW | FAILED      │
        └─────────────────────────────────────────────────────────┘
                                │
                                ▼
             ┌─── post-fix policy (optional, after DONE) ───┐
             │  working-tree | branch | branch-pr            │
             └────────────────────────────────────────────────┘
```

Each transition in the workflow loop is a row in
`<root>/.autofix/runs/<run-id>/state.jsonl` — append-only, JSONL,
byte-level atomic per O_APPEND. Concurrent `autofix run` invocations
each get a unique `run_id` directory; their logs cannot interleave.

### Repair coordination (TRIAGING + PLANNING)

`autofix.repair.coordinate_repairs(findings, threshold, root)` is the
entry point. For each finding it emits a `RepairTask` with a tier:

- **`DETERMINISTIC`** — single-name unused-import deletions. The
  apply pass rewrites the file deterministically (sibling tempfile
  + atomic rename).
- **`LLM_PATCH`** — the cheap-tier router decided this finding is a
  good candidate for an LLM-generated diff. `produce_patch(task)`
  invokes the LLM with the evidence packet, parses the response
  through the unified-diff fence contract, and validates the
  candidate diff via `git apply --check --no-unsafe-paths`.
- **`HUMAN_REVIEW`** — anything the router can't classify. Surfaced
  in the JSONL log with `reason="preview_only"`.

The threshold (default `0.6`, lives in
`autofix/cli/run_constants.py::LLM_PATCH_THRESHOLD`) is the priority
score above which findings get routed to `LLM_PATCH` instead of
`HUMAN_REVIEW`.

### LLM judgment analyzers

Three analyzer categories share a base class
`autofix.analyzers.llm_judgment.LLMJudgmentAnalyzer`:

```
LLMJudgmentAnalyzer (base, owns: caching, JSON parsing, telemetry, error recovery)
    ├── SecurityJudgmentAnalyzer        (opus, 9 OWASP categories)
    ├── CodeQualityJudgmentAnalyzer     (sonnet, 9 antipatterns)
    ├── DeadCodeJudgmentAnalyzer        (sonnet, 6 categories)
    └── PerformanceJudgmentAnalyzer     (sonnet, 11 categories)
```

Subclasses override only `RULE_ID_PREFIX`, `MODEL`, and
`prompt_template(diff_context)`. Cache key is
`sha256(prompt + commit_sha + model)`; an entry's envelope
re-validates `key`, `model`, and `commit_sha` on read to defend
against TOCTOU cache-poisoning.

Each analyzer emits open-set categories — the prompted list is the
directive, NOT a runtime whitelist. Downstream consumers treat
unknown category strings as valid-but-unknown.

### Verify state primitive

`autofix.workflow.verify.run_verification(...)` is a pure function
that drives the VERIFYING transition: it auto-detects the test
runner via marker files (`pyproject.toml`/`setup.py`/`setup.cfg`/
`package.json`/`go.mod`), honors `.autofix/config.json::test`
overrides, runs the test command via `subprocess.run` with timeout
+ log capture, then re-scans the working tree via `_run_scan_core`.
The returned `VerifyResult` carries enough math
(`unresolved_finding_ids`, `new_finding_count`, `regressed`) for
the loop driver to decide DONE vs RETRY vs FAILED.

All numeric and string constants live in
`autofix/workflow/verify_constants.py` (the no-magic-numbers
discipline established in ARCH-010 and ARCH-011 — every literal
the verify body uses must come from a named constant; a grep test
in `tests/autofix/workflow/test_verify_no_magic_numbers.py`
enforces this at test time).

### `--watch` integration

`autofix/cli/_watch_loop.py::run_watch_loop(session, dispatcher,
*, safety_sweep_seconds, once)` is shared between `autofix watch`
(scan-only — the historical behavior) and `autofix run --watch`
(full workflow loop per cycle). The dispatcher closure captures
`args` and forwards each Watchman batch's `is_fresh_instance` flag
to the per-cycle scan.

Per-cycle exception isolation is mandatory: `Exception` from the
dispatcher is caught, logged to stderr, and the loop continues.
`KeyboardInterrupt` and `SystemExit` propagate. This contract is
enforced by `tests/autofix/cli/test_watch_loop_helper.py`.

### Post-fix policy

`autofix/cli/post_fix_policy.py::apply_post_fix_policy(...)` runs
ONLY after `State.DONE` with a non-empty applied-finding set. The
function reads `.autofix/config.json::post_fix` (overridable via
`--post-fix`), creates a fresh branch
(`autofix/fixes-<run-id>`), commits the working-tree changes with
a structured message, and optionally invokes `gh pr create` for
the `branch-pr` mode. Subprocess errors are caught, the original
branch is best-effort restored, and the function returns
`working-tree` rather than raising — the run still ends `DONE`.

All policy literals (enum values, branch prefix, commit-message
templates, gh argument lists, config keys) live in
`autofix/cli/post_fix_constants.py`. A grep test
(`tests/autofix/cli/test_post_fix_no_magic_numbers.py`) verifies
the policy module body never inlines them.

## Workflow state machine internals

`autofix.workflow.StateMachine` is a producer-only class:

- One instance per `autofix run` invocation; one `run_id` per
  instance.
- `transition(to_state, evidence_sha256, reason=None)` validates
  the (from, to) pair against `_TRANSITIONS` (see
  `autofix/workflow/state_machine.py`) and appends a new
  `StateRow` to the JSONL log.
- The initial `SCANNING` row is written in `__init__` so even an
  immediate-failure run leaves a trace.
- File writes use `O_APPEND` for byte-level atomicity; readers
  parse line-by-line with `json.loads` — a half-written line is
  skipped without error.
- `StateMachine.from_log(path)` reconstructs an in-memory replay
  for diagnostic tooling.

Illegal transitions raise `InvalidTransition`. Empty / malformed /
illegal-sequence logs raise `InvalidLog` on `from_log`. Both are
public exceptions on `autofix.workflow`.

## Above the run loop: the crawl subsystem

The 5-layer funnel produces findings. The run loop consumes them.
The **crawl** is the operator-facing default of bare `autofix` —
it drives the run loop continuously over time, scanning **bundles**
(graph subsets) instead of singleton files:

```
                  ┌─────────── autofix (bare) ──────────┐
                  │   reads .autofix/config.json         │
                  │   runs the crawl driver continuously │
                  └───────────────┬──────────────────────┘
                                  │
                                  ▼
              ┌─────── crawl.driver.run_crawl_continuously ───────┐
              │  loop forever; sleep `interval_seconds` between    │
              │  cycles; pidfile at .autofix/crawl.pid             │
              └───────────────┬───────────────────────────────────┘
                              │ (per cycle)
                              ▼
       ┌──── pick bundles ────┐
       │  freshness × relevance, hub-saturation cap, bounded
       │  expansion (max_hops / max_files / max_bytes)
       └──────┬───────────────┘
              ▼
       ┌──── analyze each (bundle, analyzer) ────┐
       │  cheap + LLM analyzers; LLM cache       │
       │  re-keyed on prompt-content + commit     │
       └──────┬───────────────┘
              ▼
       ┌──── on findings + mode != preview ────┐
       │  invoke run_command._run_one_cycle    │
       │  (the existing run loop)              │
       └────────────────────────────────────────┘
```

The crawl is what runs by default when an operator types `autofix`.
The toolkit shipped in ARCH-001..015 (analyzers, repair coordinator,
LLM patcher, workflow state machine, post-fix policy) is the
**plumbing** the crawl drives.

### Crawl module map

| Module | Responsibility |
|---|---|
| `autofix.crawl.crawl_constants` | Pinned defaults (horizons, caps, weights, budget tiers, mode/budget enums). Side-effect-free. |
| `autofix.crawl.config` | Read/write `.autofix/config.json`. Resolves budget tier names to dicts. |
| `autofix.crawl.bundles` | `Bundle` dataclass + `expand_bundle` (BFS bounded by 3 caps + hub saturation). |
| `autofix.crawl.score` | `file_freshness`, `bundle_freshness`, `relevance`, `priority` — pure scoring functions. |
| `autofix.crawl.ledger` | `LedgerRow` + `Ledger` — append-only JSONL persistence with `O_APPEND` atomicity. |
| `autofix.crawl.picker` | `pick_next_batch` — deterministic bundle selection per cycle. |
| `autofix.crawl.driver` | `run_crawl_once` + `run_crawl_continuously` — loop driver with pidfile lifecycle and per-cycle exception isolation. |
| `autofix.cli.init_command` | `autofix init` interactive wizard. |
| `autofix.cli.status_command` | `autofix status` — reads pidfile + ledger, prints summary. |
| `autofix.cli.main` | Top-level dispatch: bare `autofix` routes to the crawl when `--root` is provided. Layered `--help` / `--help-advanced`. |

Full crawl architecture: [`crawling.md`](crawling.md).
Full run-loop architecture: [`workflow.md`](workflow.md).
