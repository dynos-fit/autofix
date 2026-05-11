# The crawl subsystem

This document is for power users and contributors. If you just
want to use autofix, read [getting-started.md](getting-started.md).

The crawl is the operator-facing default of bare `autofix` — a
long-running daemon that walks the repo dependency graph over
time, scanning **bundles** (seed file + bounded-radius neighbors)
instead of singleton files. Bundles give every LLM analyzer
cross-file reasoning context — caller, callee, sibling subclass —
so security/dead-code/performance analyzers can find cross-file
bugs that single-file analysis misses.

## Why bundles, not singletons

A static analyzer operates on one file because that's what it
can do cheaply. An LLM is the wrong tool to use that way — its
strength is exactly the cross-file reasoning that single-file
analysis can't do. Bundles fix that:

```
                seed file (highest priority by freshness × relevance)
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   direct     direct     sibling
   callers    callees    subclasses
```

Bound by:
- `MAX_BUNDLE_HOPS = 1`
- `MAX_BUNDLE_FILES = 5`
- `MAX_BUNDLE_BYTES = 50_000`

Whichever cap trips first wins.

## The graph has no start or end

Two follow-up answers to the design question:

**1. Start: pick a seed (per cycle), not "the start of the graph".**
Per cycle, the picker picks K seeds by `freshness × relevance`.
Those are the bundle roots for THIS cycle, not for the graph. Next
cycle picks different seeds. Over time, every file becomes a seed
often enough.

**2. End: bounded expansion, not "until the graph ends".** From
each seed, BFS outward bounded by the three caps above. We never
traverse to the connected component's boundary; we stop early.

**3. Overlap is the point.** When file X is a neighbor of seed A
in bundle-A AND a neighbor of seed B in bundle-B, the two bundles
ask the LLM different questions ("is there a bug in X-as-A's-
neighbor?" vs "is there a bug in X-as-B's-neighbor?"). Different
contexts → different prompts → different cache keys → different
verdicts. Overlap is the mechanism for cross-context coverage.

But hubs (utility modules imported by everything) would otherwise
appear in every bundle's neighbor set. So:

**4. Saturation cap.** Per-file
`bundle_appearance_count_in_window` (24h sliding window) tracked
in the ledger. `expand_bundle` drops neighbors whose count
`>= MAX_HUB_APPEARANCES` (default 3). Hub seeds are NOT skipped
— only hub-as-neighbor expansions. Hot hubs still get coverage
when they're picked as seeds; they just don't appear in every
sibling's neighbor set.

## Scoring math

```
priority(bundle) = bundle_freshness(bundle) × relevance(seed)

bundle_freshness(b, ledger, current_commit_sha) =
    max(file_freshness(f) for f in b.files)
    # Unseen files contribute 1.0 (maximally stale).

file_freshness(row, current_commit_sha) =
    1.0  if  row.last_commit_sha != current_commit_sha
    else min(1.0, age_hours / STALENESS_HORIZON_HOURS)

relevance(path) =
    0.6 * exp(-days_since_last_commit / 7)         # recency
  + 0.4 * min(1.0, commits_in_last_30d / 10)        # churn
```

All weights / horizons / caps live in
[`autofix/crawl/crawl_constants.py`](../autofix/crawl/crawl_constants.py).
The no-magic-numbers grep test
([`tests/autofix/crawl/test_crawl_no_magic_numbers.py`](../tests/autofix/crawl/test_crawl_no_magic_numbers.py))
forbids consumer modules from inlining any of them.

### Why these weights

| Subscore | Weight | Rationale |
|---|---|---|
| Recency | 0.6 | Most signal in "files I edited recently". |
| Churn | 0.4 | Hot files have more bug surface; capped at 10 commits/30d. |

Centrality (import-fanout) was an earlier subscore but was removed
when supplemental scoring signals (entrypoint boost, low-value class
penalty, oversize file penalty) became the primary structural
relevance signal — see `docs/crawling-improvements.md`.

If you want to tune them, edit `crawl_constants.py`. Adding/
removing subscores is an ARCH-* level change.

## Budget tiers

```python
BUDGET_CHEAP      = {bundles_per_cycle: 1,  interval: 1h,  analyzers: cheap+security}
BUDGET_BALANCED   = {bundles_per_cycle: 5,  interval: 30m, analyzers: cheap+security+code-quality}
BUDGET_AGGRESSIVE = {bundles_per_cycle: 20, interval: 5m,  analyzers: cheap+all 4 LLM analyzers}
```

The `autofix init` wizard maps these to one of three
menu choices ("How much should it spend per day?"). Tier
defaults are documented in
[`crawl_constants.py`](../autofix/crawl/crawl_constants.py)
and are conservative; tune them by editing the file (no separate
config knob — the budget tier IS the knob).

## The ledger

Append-only JSONL at `<root>/.autofix/crawl-ledger.jsonl`. Same
on-disk discipline as the workflow state machine: byte-level
atomic via `O_APPEND`. Multiple processes can record concurrently.

Each row:

```json
{
  "ts": "2026-05-08T12:00:00Z",
  "bundle_fingerprint": "<sha256-64-hex>",
  "seed_path": "autofix/cli/run_command.py",
  "file_paths": ["autofix/cli/run_command.py", "autofix/cli/_watch_loop.py"],
  "analyzer": "llm:security",
  "last_commit_sha": "abc123",
  "last_finding_count": 0,
  "cache_hit": false,
  "event_id": "evt_..."
}
```

The fingerprint is `sha256(canonical_json(sorted(file_paths)))`
— same file SET → same fingerprint regardless of seed identity
or expansion order.

Half-written lines (e.g., from a process killed mid-write) are
skipped with a stderr warning during `replay_from_disk()`; the
surrounding rows are still consumed.

## The picker algorithm

[`autofix/crawl/picker.py::pick_next_batch`](../autofix/crawl/picker.py):

1. Enumerate Python files via `git_log.list_python_files()` (or
   `Path.rglob('*.py')` fallback for non-git trees).
2. Score each file by `relevance`. Sort descending.
3. Take the top `bundles_per_cycle * 3` candidates (over-pick to
   give priority sort headroom — some bundles get dropped to
   saturation).
4. For each candidate seed, expand into a `Bundle` via
   `expand_bundle(...)` (with the ledger, to honor saturation).
5. Score each bundle by `priority = freshness × relevance`. Sort
   descending.
6. Take the top `bundles_per_cycle` bundles.
7. Emit one `(bundle, analyzer)` pair per analyzer in the
   resolved set.

**Determinism**: given identical inputs, the picker produces the
same bundle ordering. Verified by
[`test_picker_determinism.py`](../tests/autofix/crawl/test_picker_determinism.py).

## The driver

[`autofix/cli/cycle_runner.py`](../autofix/cli/cycle_runner.py) — two
entry points:

- `run_crawl_once(*, root, mode, budget, ...)` — one cycle, exit
  code 0 on clean completion.
- `run_crawl_continuously(*, root, mode, budget, interval_seconds,
  ...)` — loops forever; sleeps `interval_seconds` between cycles.
  Returns 0 on `KeyboardInterrupt`.

Per-cycle exception isolation: `Exception` raised by the cycle is
caught + logged to stderr + the loop continues.
`KeyboardInterrupt` and `SystemExit` propagate.

Pidfile: `.autofix/crawl.pid` is written on driver startup,
removed on clean exit. `autofix status` reads it to determine
whether a daemon is running.

## Cost shape over time

```
Cycle 0   (cold start):   ledger empty → picker scores by relevance only
Cycle 1   (~30m later):   10 (file, analyzer) pairs in ledger; pick next 10
Cycle 24  (12h):          ~40% of repo covered; some re-scans, mostly new
Cycle 48  (24h):          ~80% covered; cache hits dominate
Cycle 96+ (steady state): mostly cache hits + targeted re-scans of churned files
```

Steady-state spend = `(bundles_per_cycle × analyzers × cycle_interval × $/call)`
multiplied by `(1 - cache_hit_rate)`. For most repos with churn
on a small subset of files, cache_hit_rate is 70-90%.

## Tunables (and where they live)

Every numeric/string constant:
[`autofix/crawl/crawl_constants.py`](../autofix/crawl/crawl_constants.py).

| Knob | Default | Effect |
|---|---|---|
| `STALENESS_HORIZON_HOURS` | 24 | After this much time, a file's freshness saturates at 1.0 |
| `HUB_SATURATION_WINDOW_HOURS` | 24 | Sliding window for the per-file appearance counter |
| `MAX_HUB_APPEARANCES` | 3 | A neighbor can appear in at most N bundles in the window |
| `MAX_BUNDLE_HOPS` | 1 | BFS depth from seed |
| `MAX_BUNDLE_FILES` | 5 | Bundle size cap |
| `MAX_BUNDLE_BYTES` | 50_000 | Bundle byte budget (LLM context window protection) |
| `RELEVANCE_WEIGHT_RECENCY` | 0.6 | Subscore weight |
| `RELEVANCE_WEIGHT_CHURN` | 0.4 | Subscore weight |
| `RECENCY_DECAY_DAYS` | 7.0 | exp(-days/N) for recency |
| `CHURN_CAP_COMMITS` | 10 | Commits/30d that fully saturate churn |

## What's NOT in the crawl

Conscious exclusions:

- **No remote / multi-host crawl.** One daemon per repo.
- **No findings deduplication across bundles within a cycle.**
  The LLM cache handles this naturally via prompt-content keys;
  no extra dedup layer.
- **No "smart" cost prediction beyond budget tier defaults.** The
  operator picks `cheap`/`balanced`/`aggressive` and the
  constants module owns the math.
- **No automatic cleanup of accumulated `.autofix/runs/`
  directories.** Pre-existing concern; cleanup tooling would be a
  separate task.
- **No notification / email integration when a finding fires.**
  The operator runs `autofix status`, or `gh pr` handles it for
  `mode=pr`.

## Optional flags and modes

The crawl ships a handful of opt-in subsystems gated behind keys in
`.autofix/config.json` under a top-level `crawler.*` namespace. With
zero configuration none of them activate — the crawl behaves exactly
as documented in the sections above.

| Key | Default | Effect |
| --- | --- | --- |
| `crawler.scoring.entrypoint_boost` | `false` | Adds `ENTRYPOINT_BOOST` to relevance for `entrypoint`-class files (`__main__.py`, `manage.py`, `cli.py`, …). |
| `crawler.scoring.low_value_class_penalty` | `false` | Multiplies relevance by `LOW_VALUE_CLASS_PENALTY` for `docs`/`vendor`/`generated`/`build_output`/`lockfile`/`binary`/`cache` files. |
| `crawler.scoring.oversize_file_penalty` | `false` | Multiplies relevance by `OVERSIZE_FILE_PENALTY` when a file exceeds `MAX_RELEVANT_FILE_BYTES`. |
| `crawler.expansion.class_aware` | `false` | Switches `expand_bundle` to class-aware BFS: test seeds prioritize the impl mirror, config seeds cap at 1 hop, entrypoints get `MAX_BUNDLE_HOPS_ENTRYPOINT`, junk-sink classes are dropped. |
| `crawler.modes.impact_cone` | `false` | Seed from the working-tree diff (`git status --porcelain=v1`) instead of running the full relevance picker. Empty diff falls back to the picker. |

For per-flag semantics, observed effects, and tuning guidance, see
[`crawling-tuning.md`](crawling-tuning.md). For the full list of
modules touched in this iteration, see
[`crawling-improvements.md`](crawling-improvements.md).

### `.autofixignore`

A repo-root file with gitignore-style globs. When present, the
crawler excludes matched paths from seed candidates and neighbor
expansion. Stacks on top of `.gitignore` —
`autofixignore can only further-exclude`. Documented in
[`crawling-tuning.md`](crawling-tuning.md).

### `--debug-crawl`

Top-level CLI flag on the bare-crawl path. When set, the cycle emits
a multi-line stats breakdown (bundles built, byte-size distribution,
top seeds, budget-hit counts, drop counts) to stderr. `--quiet`
overrides `--debug-crawl`. Default off.

## Using the crawler standalone

`autofix/crawl/` is designed as a reusable subsystem — it has no
imports of any other `autofix.X` subsystem (an architectural guard
test, `tests/autofix/crawl/test_crawl_subsystem_isolation.py`,
pins this boundary). Anything `autofix.cli.cycle_runner` does on
top of the crawler is the *consumer*'s responsibility, not the
crawler's.

The public surface lives in `autofix/crawl/__init__.py::__all__`:

- `Bundle`, `expand_bundle` — bundle construction (BFS over your
  call-graph adapter, bounded by hops / files / bytes).
- `pick_next_batch` — the deterministic per-cycle selection
  algorithm.
- `Ledger`, `LedgerRow` — append-only JSONL persistence for
  cache-hit / hub-saturation accounting.
- `file_freshness`, `bundle_freshness`, `relevance`, `priority` —
  pure scoring functions.
- `GitLogAdapter`, `CallGraphAdapter` — the two Protocol contracts
  you implement.

### The two adapter Protocols

Defined in [`autofix/crawl/contracts.py`](../autofix/crawl/contracts.py).
Both are `runtime_checkable` (`isinstance(obj, GitLogAdapter)`
verifies attribute presence — note that runtime `isinstance` on
a Protocol does NOT verify method signatures; see PEP 544).

```python
from pathlib import Path
from typing import Protocol, runtime_checkable

@runtime_checkable
class GitLogAdapter(Protocol):
    def list_candidate_files(self) -> list[str]: ...
    def days_since_last_commit(self, path: str) -> int: ...
    def commits_in_last_30_days(self, path: str) -> int: ...

@runtime_checkable
class CallGraphAdapter(Protocol):
    def neighbors_of(self, path: Path) -> list[Path]: ...
```

The picker uses `GitLogAdapter` to score candidate seed paths via
`relevance(...)` (recency 0.6 + churn 0.4). `expand_bundle` uses
`CallGraphAdapter` to walk one BFS step from each seed (callers ∪
callees, deduped). Centrality was deliberately removed from the
contract (PR #93) because it required language-specific
import-graph walking; heavily-depended-on files float to the top
via churn instead.

### Reference implementations

- [`autofix/cli/cycle_runner.py::_GitLogAdapter`](../autofix/cli/cycle_runner.py)
  — git-subprocess-backed with a stdlib-only fallback (`Path.rglob`
  + `stat().st_mtime`) for non-git trees.
- [`autofix/crawl/_call_graph_adapter.py::CallGraphPathAdapter`](../autofix/crawl/_call_graph_adapter.py)
  — wraps the project's symbol-level
  `autofix.invalidation.call_graph.CallGraph` as a path-level adapter.

External adapters can substitute either without other code changes.

### Minimum viable integrator

The smallest standalone use of the crawler — picks bundles from a
non-git directory, with a no-op call graph (each bundle degenerates
to a singleton seed). Replace the adapters with real
implementations when you have git + a call-graph index.

```python
from pathlib import Path
from autofix.crawl import (
    Ledger, pick_next_batch,
    GitLogAdapter, CallGraphAdapter,
)


class StubGitLog:
    """Stdlib-only fallback — enumerate every file under root."""
    def __init__(self, root: Path) -> None:
        self.root = root

    def list_candidate_files(self) -> list[str]:
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file()
        )

    def days_since_last_commit(self, path: str) -> int:
        return 0  # all files treated as freshly modified

    def commits_in_last_30_days(self, path: str) -> int:
        return 0  # no churn signal


class NoOpCallGraph:
    """Returns no neighbors → bundles degenerate to singleton seeds."""
    def neighbors_of(self, path: Path) -> list[Path]:
        return []


root = Path("/path/to/your/repo")
ledger = Ledger(root=root)  # writes to .autofix/crawl-ledger.jsonl
bundles = pick_next_batch(
    root=root,
    ledger=ledger,
    current_commit_sha="HEAD",  # any string; used only for ledger keys
    git_log=StubGitLog(root),
    call_graph=NoOpCallGraph(),
    bundles_per_cycle=5,
)

for b in bundles:
    print(b.seed_path, "->", [f.as_posix() for f in b.file_paths])
```

`pick_next_batch` is analyzer-agnostic — it returns `Bundle`
objects only. What you do with each bundle (run analyzers, ship
to an LLM, write to a log) is your consumer's job.

## See also

- [`getting-started.md`](getting-started.md) — quick-start guide
- [`architecture.md`](architecture.md) — how the funnel + run loop
  + crawl fit together
- [`workflow.md`](workflow.md) — the `autofix run` workflow loop
  (which the crawl invokes per cycle when `mode != preview`)
- [`crawling-tuning.md`](crawling-tuning.md) — per-flag tuning guide
- [`crawling-improvements.md`](crawling-improvements.md) — list of
  modules touched in task-20260508-002
