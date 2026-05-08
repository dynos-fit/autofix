# autofix

Deterministic find-and-fix for code changes, narrated by an LLM.

`autofix` is a scanner + repair pipeline: it looks at what you committed,
runs cheap analyzers (unused imports), industry linters (ruff, mypy,
eslint, golangci), and LLM-judgment analyzers (security, code quality,
dead code, performance) — then optionally **applies** the fixes, runs
your test suite, re-scans to confirm convergence, and (optionally)
opens a PR.

```
$ autofix run --root . --apply --auto-llm
autofix: SCANNING ...
autofix: TRIAGING ...
autofix: PLANNING ...
autofix: APPLYING ...
autofix: VERIFYING ...
autofix: DONE
autofix: post-fix → branch (autofix/fixes-2026-05-08T01-23-45Z-abc123)
```

The full run-loop is the `autofix run` umbrella command. Underneath
sit smaller composable subcommands (`scan`, `fix`, `watch`, `replay`,
`export-sarif`, `policy`) that drive the same pipeline one piece at a
time.

## What ships today

| Capability | What it is |
|---|---|
| **Scan** | Diff-scoped scan via `git diff --name-only HEAD~1 HEAD` (default) or `--full-sweep`. Findings written as SARIF 2.1.0. |
| **Cheap analyzer** | `unused-import.intra-file` — Python only, AST-based, zero LLM cost. |
| **Linter passthrough** | Adapters for `ruff`, `mypy`, `eslint`, `golangci-lint`. Findings flow through the same pipeline as the cheap and LLM analyzers. |
| **LLM judgment analyzers** | Four categories — `security` (9 OWASP-style classes), `code-quality` (9 antipatterns), `dead-code` (6 categories), `performance` (11 categories). Each is a thin subclass over a shared cache + parse + telemetry base. |
| **Repair coordinator** | Routes findings to a fix-tier: deterministic deletion (cheap), LLM-generated diff (`--auto-llm`), or human review. |
| **LLM patcher** | Generates unified diffs and validates them with `git apply --check --no-unsafe-paths` before any source mutation. |
| **Workflow state machine** | Append-only JSONL log of every transition — `SCANNING → TRIAGING → PLANNING → APPLYING → VERIFYING → DONE/RETRY/HUMAN_REVIEW/FAILED`. |
| **Verify state** | Re-runs the project's tests (auto-detected via marker files) and re-scans to confirm applied fixes are gone and no new findings appeared. |
| **`--watch` mode** | Long-running daemon: subscribes to filesystem events via Watchman and runs one full workflow cycle per change batch. |
| **Branch & PR policy** | Optional post-DONE step: create `autofix/fixes-<run-id>`, commit the applied changes, and (if `gh` is installed) open a PR. |
| **Recovery branches** | Pre-apply snapshot branches let you `git checkout -` to a known-clean state if anything goes wrong during `--auto-llm`. |
| **Replay** | Re-run any past scan from its event log to reproduce a CI failure deterministically. |

The architecture upgrade roadmap (ARCH-001 through ARCH-015) is fully
shipped on `main`.

## Install

```bash
git clone https://github.com/dynos-fit/autofix
cd autofix
./install.sh
```

Creates `.venv/`, installs in editable mode, verifies the `autofix`
console script. Python 3.11 – 3.13.

Optional extras:

```bash
./install.sh --with-watch    # long-running watcher (needs `watchman` daemon)
./install.sh --with-dedup    # semantic dedup (sentence-transformers)
./install.sh --with-go       # Go adapter (auto-downloads scip-go)
./install.sh --with-jsts     # TypeScript adapter (needs scip-typescript via npm)
./install.sh --all           # everything
./install.sh --dev --all     # dev install + tests
./install.sh --help
```

## 30-second quickstart

```bash
cd /path/to/your/repo
git commit -am "your latest changes"  # autofix only scans committed code
autofix run --root .
```

Default mode is **preview-only**: nothing is touched, and the command
exits with code `3` (HUMAN_REVIEW). To actually apply the safe
deterministic deletions:

```bash
autofix run --root . --apply
```

To additionally apply LLM-generated patches for findings the
deterministic tier can't fix:

```bash
autofix run --root . --apply --auto-llm
```

A recovery branch (`autofix/pre-fix-snapshot-<utc>`) is captured
before any source mutation, so you can always rewind.

## The workflow loop

```
                    ┌────────────────┐
                    │   SCANNING     │  diff-scoped scan via the funnel
                    └───────┬────────┘
                            ▼
                    ┌────────────────┐
                    │   TRIAGING     │  coordinate_repairs → fix tier
                    └───────┬────────┘
                            ▼
                    ┌────────────────┐
                    │   PLANNING     │  produce_patch for LLM tier
                    └───────┬────────┘
                            ▼
       ┌── default ──────── ▼ ──── --apply ──┐
       │                                       │
       ▼                                       ▼
┌─────────────┐                       ┌────────────────┐
│ HUMAN_REVIEW│                       │   APPLYING     │  deterministic + LLM
└─────────────┘                       └───────┬────────┘
                                              ▼
                                      ┌────────────────┐
                                      │   VERIFYING    │  test + re-scan
                                      └───────┬────────┘
                                              ▼
                                  ┌─── clean ─┴── unresolved ───┐
                                  ▼                              ▼
                           ┌────────────┐                ┌──────────────┐
                           │    DONE    │                │   RETRY (≤3) │ ←┐
                           └────────────┘                └───────┬──────┘  │
                                                                 │         │
                                                                 └─────────┘
```

Every transition is recorded to `.autofix/runs/<run-id>/state.jsonl`.
After three failed retries the workflow ends in `FAILED` (exit code
`1`).

## Run modes

| Mode | Command | Effect |
|---|---|---|
| Preview (default) | `autofix run` | Scan + plan, print suggestions, exit code `3` |
| LLM previews | `autofix run --suggest` | Print LLM-generated diffs to stdout, no source mutation |
| Deterministic apply | `autofix run --apply` | Apply safe deletions; verify with tests |
| Full apply | `autofix run --apply --auto-llm` | Deterministic + LLM patches; recovery branch + verify |
| Long-running | `autofix run --apply --watch` | Subscribe to Watchman, run one cycle per change batch |
| Single-shot scan | `autofix scan` | Just run the analyzers; no apply pass |
| Single-shot fix | `autofix fix --apply` | Just the deterministic apply pass; no verify |

`--watch` is compatible with every flag combination above. Use
`--safety-sweep 30m` to force a full-sweep cycle when no Watchman
events arrive within the threshold.

## Analyzer set

Default is the cheap analyzer (`cheap`). Compose explicit sets via
`--analyzers`:

```bash
# Just ruff
autofix run --root . --analyzers linter:ruff

# Cheap + ruff + LLM security
autofix run --root . --analyzers cheap,linter:ruff,llm:security

# Everything
autofix run --root . \
  --analyzers cheap,linter:ruff,linter:mypy,linter:eslint,linter:golangci,llm:security,llm:code-quality,llm:dead-code,llm:performance
```

| Analyzer set | What it surfaces |
|---|---|
| `cheap` | `unused-import.intra-file` (Python) |
| `linter:ruff` | Anything ruff flags |
| `linter:mypy` | Anything mypy flags |
| `linter:eslint` | Anything eslint flags |
| `linter:golangci` | Anything golangci-lint flags |
| `llm:security` | path-traversal, sql-injection, command-injection, secret-leak, auth-bypass, unsafe-deserialization, crypto-misuse, prompt-injection, data-exposure |
| `llm:code-quality` | error-handling-gap, dead-branch, magic-number, unclear-name, overly-broad-except, missing-docstring, complexity-creep, duplicated-logic, boundary-validation-missing |
| `llm:dead-code` | unused-import, unused-export, unreferenced-file, dead-function, unused-variable, commented-out-code |
| `llm:performance` | n-plus-one, missing-index, unbounded-query, missing-transaction, algorithmic-complexity, unnecessary-recomputation, large-payload-serialization, connection-pool-exhaustion, memory-accumulation, missing-timeout, blocking-event-loop |

LLM analyzers cache by `sha256(prompt + commit_sha + model)`, so
re-running a clean scan is free.

## Configuration

A single optional file at `.autofix/config.json` controls verify and
post-fix behavior:

```json
{
  "test": {
    "command": ["pytest", "-x"],
    "timeout_seconds": 600,
    "cwd": "."
  },
  "post_fix": "branch"
}
```

| Key | Values | Default | Effect |
|---|---|---|---|
| `test.command` | `string[]` | auto-detected from project markers | Test command for the VERIFYING state |
| `test.timeout_seconds` | `int > 0` | `300` | Per-cycle test timeout |
| `test.cwd` | `string` | repo root | CWD for the test command |
| `post_fix` | `"working-tree"` / `"branch"` / `"branch-pr"` | `"working-tree"` | Post-DONE branch & PR policy |

If the file is absent, the verify state auto-detects the test runner
via `pyproject.toml` (`pytest -x`), `setup.py`/`setup.cfg`
(`pytest -x`), `package.json` (`npm test`), or `go.mod`
(`go test ./...`). When no marker is present, the verify state
emits `test_passed=None` and skips the test signal — the re-scan
still runs.

`post_fix` policy:

- `working-tree` (default) — leaves the working tree dirty after
  DONE; you stage and commit yourself.
- `branch` — creates `autofix/fixes-<run-id>`, runs `git add -A`,
  commits with a structured message linking to every applied
  finding-id, then switches back to your original branch.
- `branch-pr` — same as `branch`, then runs `gh pr create --fill
  --base <original> --head <branch>`. If `gh` isn't installed,
  degrades to `branch` with a stderr warning.

CLI override: `autofix run --post-fix branch-pr` beats whatever the
config file says.

## Examples

### Scan the latest commit

```bash
$ autofix scan --root .
sample.py:2   warning  unused-import.intra-file  Unused import: json
2 findings written to .autofix/scans/.../findings.sarif
```

### See what `autofix run --apply` would do, without doing it

```bash
$ autofix run --root . --suggest
# prints LLM-generated diffs to stdout; nothing on disk changes
```

### Long-running watcher with auto-fix and PR

```bash
$ autofix run --root . --apply --auto-llm --watch \
              --post-fix branch-pr --safety-sweep 1h
```

This is the closest thing to "background bug-fixer" the tool ships.
Every changeset that hits the working tree triggers a full
scan → fix → verify → branch → PR cycle. `--safety-sweep 1h` forces
a full cycle every hour even on a quiet repo.

### Replay a past scan

```bash
$ autofix replay --scan-id 20260506T195913Z-9f3636a9 --root .
verdict: match
scan_id: 20260506T195913Z-9f3636a9
commit_sha: a3a708cf...
```

Verdicts: `match` (deterministic; same finding ids), `mismatch`
(analyzer changed), `version_drift` (toolchain pinned-version
mismatch).

### Export SARIF after the fact

```bash
$ autofix export-sarif --scan-id 20260506T195913Z-9f3636a9 --out findings.sarif
```

For uploading to GitHub Code Scanning or feeding another tool.

### Inspect or validate your policy

```bash
$ autofix policy --show --root .
$ autofix policy --validate --root .   # exits 0 ok / 2 with diagnostics
```

The policy file is `.autofix/autofix-policy.json` (separate from
`config.json`; controls the LLM scheduler's tiering thresholds and
budgets — see [`docs/AGENTIC_LLM_BACKENDS.md`](docs/AGENTIC_LLM_BACKENDS.md)).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | DONE — workflow converged or no findings to fix |
| `1` | FAILED — max-retries exhausted, or fatal IO/scan error |
| `2` | usage error (bad flag combination) |
| `3` | HUMAN_REVIEW — preview-only mode; no source mutation |

## LLM backend

The LLM seam shells out to `claude` (Claude Code CLI) by default. If
you have `claude` on your PATH, every LLM analyzer and the LLM
patcher work automatically — no configuration needed.

Other backends (OpenAI, Ollama, vLLM, etc.) are supported by the
underlying `autofix.llm_backend` library. To switch backends, set
the relevant environment variables in your shell wrapper around
`autofix`. See [`docs/AGENTIC_LLM_BACKENDS.md`](docs/AGENTIC_LLM_BACKENDS.md)
for the full reference.

If no LLM backend is reachable, autofix still runs end-to-end. You
get the deterministic findings (cheap + linter passthrough) and the
SARIF; LLM analyzers and `--auto-llm` patches are skipped with a
once-per-scan `AnalyzerUnavailable` event in the log.

## On-disk layout

```
<repo>/.autofix/
  events.jsonl                       # append-only event log (used for replay)
  config.json                        # optional: test + post_fix policy
  autofix-policy.json                # optional: LLM scheduler policy
  scans/<scan-id>/findings.sarif     # per-scan SARIF output
  runs/<run-id>/state.jsonl          # workflow state machine log
  cache/llm_judgment/<key>.json      # per-finding LLM cache
  state/index/                       # SCIP symbol/reference index
  state/embedding-sidecar/           # semantic dedup index (--with-dedup)
```

Recommended `.gitignore`:

```
.autofix/scans/
.autofix/runs/
.autofix/state/
.autofix/cache/
```

Keep `.autofix/config.json` and `.autofix/autofix-policy.json`
checked in — they're project policy.

## Multi-language

Python is the default. Go and TypeScript adapters auto-install
their SCIP backends:

```bash
./install.sh --with-go    # autofix scan --root /your/go/repo
./install.sh --with-jsts  # autofix scan --root /your/ts/repo
```

The Go adapter auto-downloads `scip-go` on first use. TypeScript
needs `scip-typescript` from npm:

```bash
npm install -g @sourcegraph/scip-typescript
```

Without these binaries, the language adapter falls back to a
Tree-sitter-only path (works, less precise).

## Cron / CI

Hourly cron:

```cron
0 * * * * cd /repo && /path/to/.venv/bin/autofix run --root . --apply \
  >> /var/log/autofix.log 2>&1
```

GitHub Actions:

```yaml
- run: |
    pip install -e .
    autofix scan --root .
- uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: .autofix/scans/*/findings.sarif
```

## Command reference

```
autofix run            # full workflow: scan → triage → plan → (apply) → verify → done
autofix scan           # just the analyzers (default: HEAD~1..HEAD)
autofix fix            # just the apply pass (deterministic + optional LLM)
autofix watch          # long-running scanner (needs watchman)
autofix replay         # reproduce a past scan
autofix export-sarif   # write SARIF for a past scan
autofix policy         # inspect or validate .autofix/autofix-policy.json
```

Every subcommand accepts `--help`. The umbrella `autofix run` is the
recommended entry point for new users; the focused subcommands are
useful when you want to wire one phase into a larger pipeline.

## Requirements

- Python 3.11, 3.12, or 3.13
- `git` (the tool reads `git diff`)
- `claude` CLI **or** any OpenAI-compatible endpoint (optional, for LLM analyzers)
- `watchman` daemon (only for `--watch`)
- `gh` CLI (only for `post_fix=branch-pr`)
- `mypy` / `ruff` / `eslint` / `golangci-lint` (only when invoked via the matching `linter:*` analyzer)

## Documentation

- [`docs/AGENTIC_LLM_BACKENDS.md`](docs/AGENTIC_LLM_BACKENDS.md) — LLM backend setup
- [`docs/AUTOFIX_STANDALONE.md`](docs/AUTOFIX_STANDALONE.md) — operations runbook
- [`docs/architecture.md`](docs/architecture.md) — internals: funnel, SCIP, SARIF, replay, state machine
- [`docs/workflow.md`](docs/workflow.md) — the run loop, verify state, post-fix policy, recovery branches
- [`CHANGELOG.md`](CHANGELOG.md) — release notes

## Contributing

```bash
./install.sh --dev --all
ruff check autofix/                # lint
mypy autofix/ --ignore-missing-imports   # type-check
pytest tests/autofix/              # full test suite
```

Bug reports and PRs welcome at https://github.com/dynos-fit/autofix.

## License

MIT.
