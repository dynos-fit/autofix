# autofix-scanner

Standalone codebase scanner with two CLIs:

- **`autofix-next`** — clean-slate, deterministic, git-scoped scanner. The
  recommended entry point for new work. Five subcommands: `scan`,
  `replay`, `export-sarif`, `watch`, `policy`. State and policy live
  under the locked `.autofix/` tree; new artifacts land under
  `.autofix-next/`.
- **`autofix`** (legacy) — the original repository scanner and Dynos
  repair runner. Continues to function side-by-side with `autofix-next`
  through the cutover window. Retirement schedule:
  [`docs/rewrite/cli-retirement.md`](docs/rewrite/cli-retirement.md).

The full rewrite roadmap is at
[`docs/rewrite/roadmap.md`](docs/rewrite/roadmap.md). All twelve roadmap
tasks are complete; the new loop is feature-complete and tested
(563 tests passing, 0 blocking findings on the final audit).

---

## Install

```bash
./install.sh                    # base install (scan, replay, export-sarif, policy)
./install.sh --with-watch       # + Watchman-backed long-running watcher
./install.sh --with-dedup       # + sentence-transformers / hnswlib semantic dedup
./install.sh --with-jsts        # + TypeScript adapter
./install.sh --with-go          # + Go adapter
./install.sh --with-otlp        # + OpenTelemetry OTLP exporter
./install.sh --dev              # + jsonschema (test extras)
./install.sh --all              # everything except --dev
./install.sh --help             # full flag reference
```

The script creates a venv at `.venv/` (override with `--venv <path>` or
disable with `--no-venv`), installs the package in editable mode, and
verifies both `autofix` and `autofix-next` console scripts resolve.

**Python version**: 3.11 or 3.12. The current dep pins
(`tree-sitter-python<0.22`) don't ship 3.13 wheels. Override the
auto-detected interpreter with `PYTHON=python3.13 ./install.sh` once the
pins move forward.

**For `--with-watch`**: also install the Watchman daemon binary
(`brew install watchman` on macOS, `apt install watchman` on
Debian/Ubuntu, [official docs](https://facebook.github.io/watchman/docs/install.html)).
Without it, `autofix-next watch` fails fast with a clear diagnostic and
the other subcommands are unaffected.

---

## Usage — `autofix-next`

### One-off scan

```bash
cd /path/to/your/repo
autofix-next scan --root .                  # diff HEAD~1..HEAD
autofix-next scan --root . --full-sweep     # every tracked *.py
```

Outputs:

- `.autofix-next/scans/<scan-id>/findings.sarif` — SARIF 2.1.0 with
  stable `partialFingerprints` across line moves.
- `.autofix/events.jsonl` — append-only envelope rows (the locked
  observability surface).

Working-tree edits are ignored on purpose; commit first.

### Inspect the policy

```bash
autofix-next policy --show --root .       # pretty-print sorted JSON
autofix-next policy --validate --root .   # type-check 4 known top-level keys
```

The policy lives at `.autofix/autofix-policy.json` (locked, optional;
absence falls back to defaults).

### Replay a past scan (no LLM, no writes)

```bash
autofix-next replay --scan-id <scan-id-from-events.jsonl> --root .
```

Reproduces the historical scan deterministically and reports
`match` / `mismatch` / `version_drift`. Used to debug CI failures.

### Export SARIF for a past scan

```bash
autofix-next export-sarif --scan-id <scan-id> --out findings.sarif
```

### Long-running watcher

Requires `--with-watch` and a running `watchman` daemon.

```bash
autofix-next watch --root . --safety-sweep 30m
```

Watchman's `is_fresh_instance` signal flows into the change detector to
trigger a bounded full sweep on cold starts. `--safety-sweep <Nh|Nm>`
forces a full sweep when the wall-clock delta since the last one
exceeds the threshold (e.g. `30m`, `1h`, `24h`).

---

## Usage — legacy `autofix`

The legacy CLI continues to work in parallel. Twelve subcommands are
mapped 1:1 to `autofix-next` equivalents in
[`docs/rewrite/cli-retirement.md`](docs/rewrite/cli-retirement.md).
Highlights:

```bash
autofix scan --root /path/to/repo                  # → autofix-next scan
autofix policy --root /path/to/repo                # → autofix-next policy --show
autofix daemon start --root /path/to/repo          # → autofix-next watch
autofix list --root /path/to/repo                  # → autofix-next show finding (planned)
```

Cron example (legacy, hourly):

```cron
0 * * * * cd /path/to/autofix-standalone && \
  /path/to/autofix-standalone/.venv/bin/autofix scan \
  --root /path/to/target-repo >> /var/log/autofix.log 2>&1
```

For dry-run (no PRs/issues opened):

```cron
0 * * * * cd /path/to/autofix-standalone && \
  /path/to/autofix-standalone/.venv/bin/autofix scan \
  --root /path/to/target-repo --dry-run >> /var/log/autofix.log 2>&1
```

---

## On-disk layout

```
<target-repo>/
  .autofix/                          # locked, shared by both CLIs
    autofix-policy.json              #   policy (read-only from autofix-next)
    events.jsonl                     #   append-only event envelope
    state/current/findings.json      #   legacy aggregate state
    state/history/<scan-id>/         #   legacy historical snapshots
    scans/<scan-id>/                 #   legacy per-scan artifacts
  .autofix-next/                     # autofix-next only
    scans/<scan-id>/findings.sarif   #   SARIF outputs
    state/index/                     #   SCIP shards
    state/embedding-sidecar/         #   HNSW ANN index (with --with-dedup)
```

`.autofix/**` is a locked surface — `autofix-next` never writes to it
except through the documented `events.jsonl` append seam.

---

## Architecture

The new loop is a 5-layer funnel: event ingress → incremental code
intelligence → deterministic analyzers → ranking + dedup → LLM
explanation. Reference docs:

- [`docs/rewrite/target-architecture.md`](docs/rewrite/target-architecture.md) — module boundaries, language registry, clean-slate CLI surface, deprecated CLI surface, locked surfaces, config compatibility.
- [`docs/rewrite/roadmap.md`](docs/rewrite/roadmap.md) — the 12 sequenced migration tasks (all DONE).
- [`docs/rewrite/cli-retirement.md`](docs/rewrite/cli-retirement.md) — legacy → `autofix-next` mapping + T+0/T+30/T+90 retirement calendar + operator FAQ.
- [`docs/rewrite/rollback.md`](docs/rewrite/rollback.md) — how to disable `autofix-next` and verify the legacy CLI still works.

---

## LLM backends (legacy)

The legacy `autofix` supports two backends through repo-local config:
`claude_cli` and `openai_compatible`. See
[`docs/AGENTIC_LLM_BACKENDS.md`](docs/AGENTIC_LLM_BACKENDS.md).
Example local-model setup:

```bash
python3 -m autofix config set --root /path/to/repo llm_backend openai_compatible
python3 -m autofix config set --root /path/to/repo llm_base_url http://127.0.0.1:11434/v1
python3 -m autofix config set --root /path/to/repo llm_api_key ollama
python3 -m autofix config set --root /path/to/repo review_model qwen2.5-coder:7b-16k
python3 -m autofix config set --root /path/to/repo fix_model qwen2.5-coder:7b-16k
```

`autofix-next` reuses the locked LLM seam at
`autofix.llm_backend.run_prompt`, so backend configuration is shared
across both CLIs.

---

## Benchmarking

The benchmark integration lives under
[`benchmarks/agent_bench/`](benchmarks/agent_bench). The adapter
contract (`build_agent(model, max_steps, timeout) -> AgentCallable`)
is preserved byte-identically across the rewrite; existing fixtures
keep running against either loop.

```bash
AUTOFIX_BENCH_BACKEND=claude_cli \
conda run -n autofix python -m agent_bench run \
  --adapter benchmarks.agent_bench.autofix_adapter:build_agent \
  --fixtures /path/to/agent-bench/fixtures/python_small \
  --only bugfix_take_limit \
  --model default
```

For `openai_compatible`:

```bash
export AUTOFIX_BENCH_BACKEND=openai_compatible
export AUTOFIX_BENCH_BASE_URL=http://127.0.0.1:11434/v1
export AUTOFIX_BENCH_API_KEY=ollama
```

---

## Requirements

- Python **3.11** or **3.12** (3.13 blocked by current pins; see Install)
- `git`
- `gh` for issues and PRs (legacy autofix only)
- `claude` for autonomous fixes (when using the `claude_cli` backend)
- `watchman` daemon (only for `autofix-next watch`)

---

## Development

```bash
./install.sh --dev --all          # everything + test extras
pytest tests/autofix_next/        # 563 passing, 11 skipped, 2 env-dependent (scip-go/scip-python binaries)
pytest tests/                     # full suite
```

The test suite enforces:

- Locked-surface contracts (no writes to `autofix/llm_io/**`,
  `autofix/agent_loop.py`, `autofix/llm_backend.py`, `.autofix/state/**`,
  `.autofix/autofix-policy.json`, `.autofix/events.jsonl`,
  `benchmarks/agent_bench/**`).
- Deferred-import discipline for `pywatchman` (`autofix-next` loads
  cleanly on hosts without watchman).
- Stable SARIF fingerprints across line-move-only commits.
- Tier-1 dedup match semantics on shared `finding_id`.
- Security regression tests for legacy-state ingress (path traversal,
  rule_id allowlist).

---

## Operations

See [`docs/AUTOFIX_STANDALONE.md`](docs/AUTOFIX_STANDALONE.md) for the
operations runbook covering both CLIs.
