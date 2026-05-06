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
