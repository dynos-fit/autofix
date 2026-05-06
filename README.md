# autofix

Deterministic, git-scoped codebase scanner. Reads a commit-range
changeset, runs incremental analysis through a 5-layer funnel, and
emits SARIF + an append-only event log.

```bash
autofix scan --root .              # diff HEAD~1..HEAD
autofix scan --root . --full-sweep # every tracked *.py
autofix policy --show --root .     # pretty-print .autofix/autofix-policy.json
autofix replay --scan-id <id>      # reproduce a past scan deterministically
autofix export-sarif --scan-id <id> --out findings.sarif
autofix watch --root . --safety-sweep 30m   # Watchman-backed long-running scanner
```

## Install

```bash
./install.sh                     # base install
./install.sh --with-watch        # + Watchman-backed watcher
./install.sh --with-dedup        # + sentence-transformers / hnswlib semantic dedup
./install.sh --with-jsts         # + TypeScript adapter
./install.sh --with-go           # + Go adapter
./install.sh --with-otlp         # + OpenTelemetry OTLP exporter
./install.sh --dev               # + jsonschema (test extras)
./install.sh --all               # everything except --dev
./install.sh --help              # full flag reference
```

The script creates a venv at `.venv/` (override with `--venv <path>`
or disable with `--no-venv`), installs the package in editable mode,
and verifies the `autofix` console script resolves.

**Python**: 3.11, 3.12, or 3.13.

**For `--with-watch`**: also install the Watchman daemon binary
(`brew install watchman` on macOS, `apt install watchman` on
Debian/Ubuntu, [official docs](https://facebook.github.io/watchman/docs/install.html)).
Without it, `autofix watch` fails fast with a clear diagnostic; the
other subcommands are unaffected.

## Subcommands

### `autofix scan`

```bash
autofix scan --root .                  # diff HEAD~1..HEAD, *.py only
autofix scan --root . --full-sweep     # every tracked *.py
autofix scan --root . --fresh-instance # bounded full sweep over known graph symbols
```

Outputs:

- `.autofix/scans-next/<scan-id>/findings.sarif` — SARIF 2.1.0 with
  stable `partialFingerprints` across line moves.
- `.autofix/events.jsonl` — append-only envelope rows.

Working-tree edits are ignored on purpose; commit first.

### `autofix policy`

```bash
autofix policy --show --root .       # pretty-print sorted JSON
autofix policy --validate --root .   # type-check 4 known top-level keys
```

The policy file lives at `.autofix/autofix-policy.json` (optional;
absence falls back to defaults).

### `autofix replay`

```bash
autofix replay --scan-id <scan-id>   # reproduce historical scan; reports match | mismatch | version_drift
```

Used to debug CI failures. No LLM, no writes.

### `autofix export-sarif`

```bash
autofix export-sarif --scan-id <scan-id> --out findings.sarif
```

Reconstructs SARIF for a previously recorded scan from
`.autofix/events.jsonl`.

### `autofix watch`

```bash
autofix watch --root . --safety-sweep 30m
```

Watchman-backed long-running scanner. The Watchman `is_fresh_instance`
signal flows into the change detector to trigger a bounded full sweep
on cold starts. `--safety-sweep <Nh|Nm>` forces a full sweep when the
wall-clock delta since the last one exceeds the threshold.

## On-disk layout

```
<repo>/
  .autofix/
    autofix-policy.json              # policy (read-only)
    events.jsonl                     # append-only event envelope
    state/current/findings.json      # legacy snapshot (read-only; consumed by migration)
    state/index/                     # SCIP shards
    state/embedding-sidecar/         # HNSW ANN index (with --with-dedup)
    scans-next/<scan-id>/            # SARIF + per-scan artifacts
```

## Architecture

A 5-layer funnel:

1. **Event ingress** — git diff → ChangeSet
2. **Incremental code intelligence** — Tree-sitter parse, SCIP
   symbol/reference index, embedding sidecar, call graph
3. **Deterministic analyzers** — cheap (lint/regex), semantic (when
   the index is hot), impact estimator
4. **Ranking + triage** — priority scorer, 3-tier dedup (exact
   fingerprint, SimHash, embedding cosine), suppression engine,
   evidence packet builder
5. **LLM explanation** — tiered scheduler (small-model triage,
   large-model report writer), prompt-prefix cache

The full pipeline is reproducible from `.autofix/events.jsonl`
(`autofix replay`).

## LLM backend

```bash
python3 -m autofix config set --root /path/to/repo llm_backend openai_compatible
python3 -m autofix config set --root /path/to/repo llm_base_url http://127.0.0.1:11434/v1
python3 -m autofix config set --root /path/to/repo llm_api_key ollama
python3 -m autofix config set --root /path/to/repo review_model qwen2.5-coder:7b-16k
python3 -m autofix config set --root /path/to/repo fix_model qwen2.5-coder:7b-16k
```

See [`docs/AGENTIC_LLM_BACKENDS.md`](docs/AGENTIC_LLM_BACKENDS.md).

## Benchmarking

The benchmark integration lives under
[`benchmarks/agent_bench/`](benchmarks/agent_bench). The adapter
contract (`build_agent(model, max_steps, timeout) -> AgentCallable`)
exercises the real review and fix loops via `autofix.agent_loop`.

```bash
AUTOFIX_BENCH_BACKEND=claude_cli \
conda run -n autofix python -m agent_bench run \
  --adapter benchmarks.agent_bench.autofix_adapter:build_agent \
  --fixtures /path/to/agent-bench/fixtures/python_small \
  --only bugfix_take_limit \
  --model default
```

## Requirements

- Python **3.11**, **3.12**, or **3.13**
- `git`
- `gh` for issues and PRs (CI integration)
- `claude` for autonomous fixes (when using the `claude_cli` backend)
- `watchman` daemon (only for `autofix watch`)

## Development

```bash
./install.sh --dev --all
pytest tests/autofix/
```

The test suite enforces:

- Stable SARIF fingerprints across line-move-only commits.
- Deferred-import discipline for `pywatchman` (the package loads on
  hosts without watchman).
- Tier-1 dedup match semantics on shared `finding_id`.
- Security regression tests for legacy-state ingress (path traversal,
  rule_id allowlist).
- Read-only contract on `.autofix/state/current/findings.json` and
  `.autofix/autofix-policy.json`.

## Operations

See [`docs/AUTOFIX_STANDALONE.md`](docs/AUTOFIX_STANDALONE.md).
