# autofix

A continuous, cost-aware bug scanner. Walks your repo dependency
graph, narrates findings via an LLM, and (optionally) opens PRs.

```bash
$ autofix init       # one-time: pick mode + budget
$ autofix start      # daemonize: runs continuously in the background
$ autofix status     # peek at what it's doing right now
```

Plus `autofix logs` to tail the daemon log and `autofix stop` to halt it.

## What it does

`autofix` is the operator-facing default of bare `autofix` — a
long-running daemon that scans the repo over time in **bundles**
(seed file + bounded-radius neighbors). Bundles give every LLM
analyzer cross-file reasoning context (caller, callee, sibling
subclass), so security/dead-code/performance analyzers can find
cross-file bugs that single-file scans miss.

| Capability | What ships |
|---|---|
| **Crawl** | Bundle-driven graph walk with freshness × relevance picker, hub-saturation cap, ledger-keyed cache (re-scanning unchanged files is free) |
| **Cheap analyzer** | `unused-import.intra-file` (Python, AST-based, free) |
| **Linter passthrough** | `ruff`, `mypy`, `eslint`, `golangci-lint` — adapters that flow findings through the same pipeline |
| **LLM judgment analyzers** | `security` (9 OWASP-style classes), `code-quality` (9 antipatterns), `dead-code` (6 categories), `performance` (11 categories) |
| **Repair coordinator** | Routes findings to a fix tier: deterministic deletion (cheap), LLM diff (`--auto-llm`), or human review |
| **LLM patcher** | Generates unified diffs and validates them with `git apply` against the working tree before any source mutation |
| **Workflow loop** | `SCANNING → TRIAGING → PLANNING → APPLYING → VERIFYING → DONE/RETRY/HUMAN_REVIEW/FAILED` (every transition recorded in `.autofix/runs/<run-id>/state.jsonl`) |
| **Verify state** | Re-runs your tests (auto-detects pytest/jest/go-test or override via config) and re-scans to confirm fixes converged |
| **Branch & PR** | Optional post-DONE: create `autofix/fixes-<run-id>`, commit, optionally `gh pr create` |
| **Recovery branches** | Pre-apply snapshot at `autofix/pre-fix-snapshot-<utc>` lets you `git checkout` back to a known-clean state |
| **Replay** | Re-run any past scan deterministically from its event log |

## Install

```bash
git clone https://github.com/dynos-fit/autofix
cd autofix
./install.sh
```

Python 3.11–3.13. You'll also want `git` (autofix reads
`git diff`) and the `claude` CLI on your PATH. `gh` is needed only
for opening PRs.

Optional extras: `./install.sh --with-watch` (Watchman daemon),
`--with-go` (auto-downloads `scip-go`), `--with-jsts`
(needs `scip-typescript` from npm).

## 30-second quickstart

```bash
$ cd /path/to/your/repo
$ autofix init
What should autofix do?
  [1] Find bugs and open PRs for fixes (recommended)
  [2] Find bugs but never modify code
  [3] Find bugs and commit fixes directly to current branch
Choice (default: 1):

How much should it spend per day?
  [1] Cheap        (~$0.50/day, 1 bundle/cycle, 60-min interval)
  [2] Balanced     (~$2/day,    5 bundles/cycle, 30-min interval) (recommended)
  [3] Aggressive   (~$10/day,   20 bundles/cycle, 5-min interval)
Choice (default: 2):

✓ wrote .autofix/config.json

$ autofix start
autofix: daemon started (PID 12345)
$ autofix logs
autofix: cycle picked 10 (bundle, analyzer) pairs
... (tails the daemon log)
```

Press enter on each `init` question for the recommended defaults
(PR mode + balanced budget). Run `autofix start` and walk away.

For full details: [`docs/getting-started.md`](docs/getting-started.md).

## How the crawl works

```
                 ┌─────────────── ledger ────────────────┐
                 │  per (bundle, analyzer):               │
                 │    last_scanned_at, last_commit_sha,   │
                 │    last_finding_count, file_paths      │
                 └───────────────┬────────────────────────┘
                                 │
                                 ▼
              ┌──── picker (every cycle) ────┐
              │  priority = freshness × relevance      │
              │  pick top K under per-cycle budget    │
              │  expand each into a bounded bundle    │
              │  drop hub neighbors over saturation   │
              └────────────────┬───────────────────────┘
                               │
                               ▼
                  ┌─── analyze the K bundles ───┐
                  │  → findings, cache writes  │
                  │  → ledger updates          │
                  └────────────────────────────┘
                               │
                               ▼
                  if any findings + mode != preview:
                  triage → plan → apply → verify
                  (the existing run loop)
```

Over time, the whole repo gets covered, but each cycle is bounded.
Hot files re-visit sooner; untouched files eventually rotate in;
unchanged files hit the cache for free. **Cost is bounded by how
much your code changes, not by repo size.**

Architecture deep-dive: [`docs/crawling.md`](docs/crawling.md).

## Power-user reference

The dumb-user surface is intentionally tiny. If you want fine
control, the existing one-shot subcommands are still there and
unchanged:

```bash
autofix --help-advanced       # full subcommand + flag reference

autofix scan                  # one-shot scan, write SARIF, exit
autofix run                   # one-shot full workflow, exit
autofix fix                   # one-shot apply pass, exit
autofix watch                 # event-driven (Watchman) — alternative to crawl
autofix replay                # reproduce a past scan deterministically
autofix export-sarif          # re-emit SARIF for a past scan
autofix policy                # validate .autofix/autofix-policy.json
```

Every subcommand accepts `--help` for its full flag list.

For the run loop's state machine semantics, retry rules, post-fix
policy options: [`docs/workflow.md`](docs/workflow.md).

## Configuration

`.autofix/config.json` (created by `autofix init`):

```json
{
  "version": 1,
  "mode": "pr",
  "budget": "balanced",
  "test": {
    "command": ["pytest", "-x"],
    "timeout_seconds": 600
  }
}
```

| Key | Values | What it does |
|---|---|---|
| `mode` | `preview` / `commit` / `pr` | What happens after a finding fires |
| `budget` | `cheap` / `balanced` / `aggressive` | Bundles/cycle + interval + analyzer set |
| `test.command` | `string[]` | Override the auto-detected test runner for the VERIFYING state |
| `test.timeout_seconds` | `int` | Per-cycle test timeout (default 300) |

Power-user knobs (sub-budget tunables, scoring weights, hub
saturation thresholds) live in
[`autofix/crawl/crawl_constants.py`](autofix/crawl/crawl_constants.py).
Edit the file directly if you need to tune them.

### Optional crawler flags

All off by default — flip any of these in `.autofix/config.json`
under a top-level `crawler` key to enable. With every flag false
(or the `crawler` section absent entirely) the crawl is byte-
identical to the baseline.

```json
{
  "version": 1,
  "mode": "pr",
  "budget": "balanced",
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

| Flag | Effect when true |
|---|---|
| `scoring.entrypoint_boost` | Boost relevance for `__main__.py`, `manage.py`, `wsgi.py`, `cli.py`, `app.py`, console-script paths |
| `scoring.low_value_class_penalty` | Downrank docs / lockfile / vendor / generated / build_output / cache / binary files |
| `scoring.oversize_file_penalty` | Downrank files larger than `MAX_RELEVANT_FILE_BYTES` (200KB default) |
| `expansion.class_aware` | Bundle expander prefers test→impl mapping; treats config seeds as 1-hop; expands entrypoints by 2 hops; drops junk-sink neighbors |
| `modes.impact_cone` | When the working tree differs from `HEAD`, skip the relevance picker and seed bundles from the changed files instead |

See [`docs/crawling-tuning.md`](docs/crawling-tuning.md) for
per-flag tuning notes.

### `.autofixignore`

Optional `.autofixignore` at the repo root excludes paths from the
crawl. Same syntax as `.gitignore` (uses `pathspec` under the hood).
The file is additive on top of `.gitignore` — autofixignore can
only further-exclude, never un-exclude something `.gitignore`
already hid.

```gitignore
# .autofixignore
generated/
*.snapshot.json
docs/draft-*
```

If the file is absent, behavior is unchanged.

### Debug output

`autofix --root . --once --debug-crawl` (or `--debug-crawl` on the
bare `autofix --root . [--apply]` foreground crawl) emits per-cycle
stats on stderr — top seeds with score breakdowns, bundle byte
distribution, budget-hit reasons, skip counts. Useful when tuning
the optional flags above. The `autofix start` daemon does not
forward `--debug-crawl`; use the foreground forms when you need
the debug stream.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean completion (or KeyboardInterrupt for the daemon) |
| `1` | Failure — max-retries exhausted, scan/IO error |
| `2` | Usage error — bad flag combination |
| `3` | Human-review (preview-only mode) |

## On-disk layout

```
<repo>/.autofix/
  config.json                        # written by autofix init
  crawl-ledger.jsonl                 # append-only crawl ledger
  crawl.pid                          # daemon pidfile (removed on exit)
  events.jsonl                       # append-only event log (replay source)
  autofix-policy.json                # LLM scheduler policy (optional)
  scans/<scan-id>/findings.sarif     # per-scan SARIF
  runs/<run-id>/state.jsonl          # workflow state machine log
  cache/llm_judgment/<key>.json      # per-finding LLM cache
```

Recommended `.gitignore`:

```
.autofix/scans/
.autofix/runs/
.autofix/cache/
.autofix/crawl.pid
.autofix/crawl-ledger.jsonl
```

Keep `.autofix/config.json` and `.autofix/autofix-policy.json`
checked in — they're project policy.

## Multi-language

Python is first-class (Tree-sitter, no external binary). For Go
or TypeScript, install the matching adapter:

```bash
./install.sh --with-go    # auto-downloads scip-go
./install.sh --with-jsts  # needs `scip-typescript` from npm
```

Without external SCIP binaries, the language adapter falls back to
a Tree-sitter-only path (works, less precise).

## Documentation

- [`docs/getting-started.md`](docs/getting-started.md) — dumb-user guide
- [`docs/crawling.md`](docs/crawling.md) — crawl architecture, scoring, tunables
- [`docs/workflow.md`](docs/workflow.md) — run-loop state machine, post-fix policy
- [`docs/architecture.md`](docs/architecture.md) — internals: 5-layer funnel, SCIP, SARIF, replay
- [`docs/AGENTIC_LLM_BACKENDS.md`](docs/AGENTIC_LLM_BACKENDS.md) — LLM backend setup
- [`docs/AUTOFIX_STANDALONE.md`](docs/AUTOFIX_STANDALONE.md) — operations runbook

## Contributing

```bash
./install.sh --dev --all
ruff check autofix/
mypy autofix/ --ignore-missing-imports
pytest tests/autofix/
```

Bug reports and PRs welcome at https://github.com/dynos-fit/autofix.

## License

MIT.
