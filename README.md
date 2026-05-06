# autofix

A code scanner for `git diff`. It looks at what you changed, finds
problems, and asks an LLM to explain them.

```
$ autofix scan --root .
calculator.py:1   warning  unused-import.intra-file  Unused import: os
calculator.py:3   warning  unused-import.intra-file  Unused import: sys
calculator.py:4   warning  unused-import.intra-file  Unused import: List
calculator.py:4   warning  unused-import.intra-file  Unused import: Dict
calculator.py:4   warning  unused-import.intra-file  Unused import: Optional

5 findings written to .autofix/scans/20260506T195913Z-9f3636a9/findings.sarif
```

With an LLM backend configured, every finding also gets a one-paragraph
explanation: what it is, why it matters in your context, and whether
it's safe to remove.

## Status

**Alpha.** Today it ships:

- One Python analyzer: `unused-import.intra-file`
- A real LLM scheduler that routes findings through Claude, GPT, or any
  OpenAI-compatible endpoint (Ollama, llama.cpp, vLLM)
- SARIF 2.1.0 output you can drop into GitHub Code Scanning, Sonar, or
  any CI dashboard
- A long-running watcher backed by Watchman
- Deterministic replay — re-run any past scan from the event log to
  debug a flaky CI failure

What's on the roadmap (the funnel architecture is already built; only
the analyzers are missing): security checks, dead-code detection, and
cross-file semantic analyzers via SCIP for Python, Go, and TypeScript.

If you need a finished linter today, use [ruff](https://docs.astral.sh/ruff/)
or [semgrep](https://semgrep.dev/). If you want to see how an LLM-narrated
scanner feels and you're comfortable on alpha, keep reading.

## Install

```bash
git clone https://github.com/dynos-fit/autofix
cd autofix
./install.sh
```

Creates `.venv/`, installs in editable mode, verifies the `autofix`
console script. Python 3.11–3.13.

Optional extras:

```bash
./install.sh --with-watch    # long-running watcher (needs `watchman` daemon)
./install.sh --with-dedup    # semantic dedup (sentence-transformers)
./install.sh --all           # everything
./install.sh --help          # full flag list
```

## 30-second quickstart

```bash
cd /path/to/your/python/repo
git commit -am "your latest changes"     # autofix only scans committed code
autofix scan --root .
```

That's it. Findings land in:

- `.autofix/scans/<scan-id>/findings.sarif` — machine-readable
- stdout — human-readable summary

## Examples

### Scan the latest commit

```bash
$ autofix scan --root .
sample.py:2   warning  unused-import.intra-file  Unused import: json
2 findings written to .autofix/scans/...
```

### Scan everything (not just the diff)

```bash
$ autofix scan --root . --full-sweep
```

Useful for the first run on a new repo, or after a long gap.

### Inspect or validate your policy

```bash
$ autofix policy --show --root .
{
  "llm_tiered": false,
  "min_priority_for_llm_triage": 2
}

$ autofix policy --validate --root .
# exits 0 on success, 2 with diagnostics on a bad policy file
```

The policy file is `.autofix/autofix-policy.json`. It's optional.

### Replay a past scan (for debugging)

CI reported a finding you can't reproduce locally? Replay the exact
scan from its event log:

```bash
$ autofix replay --scan-id 20260506T195913Z-9f3636a9 --root .
verdict: match
scan_id: 20260506T195913Z-9f3636a9
commit_sha: a3a708cf...
```

Verdicts: `match` (deterministic; same finding ids), `mismatch`
(something changed in the analyzer), `version_drift` (your toolchain
changed — pinned versions don't match).

### Export SARIF after the fact

```bash
$ autofix export-sarif --scan-id 20260506T195913Z-9f3636a9 --out findings.sarif
```

For uploading to GitHub Code Scanning or feeding another tool.

### Run continuously (with Watchman)

```bash
$ autofix watch --root . --safety-sweep 30m
```

Re-scans on every commit. The `--safety-sweep` flag forces a full
sweep if no incremental scan has run in the last 30 minutes — protects
against subtle desync bugs.

## LLM backend

Today, the LLM seam shells out to `claude` (Claude Code CLI) by
default. If you have `claude` on your PATH, scans get explanations
automatically — no configuration needed.

Other backends (OpenAI, Ollama, vLLM, etc.) are supported by the
underlying library (`autofix.llm_backend`) but there is no operator-
facing CLI to switch them yet. Tracking issue:
[#TODO](https://github.com/dynos-fit/autofix/issues). Workaround:
edit `.autofix/autofix-policy.json` for thresholds and budgets, then
configure the backend by environment variables read by your shell
wrapper around `autofix scan`.

If you don't have an LLM backend, autofix still runs end-to-end. You
get the SARIF and the deduped findings; you just don't get the
narrated explanations.

More on the policy file shape: [`docs/AGENTIC_LLM_BACKENDS.md`](docs/AGENTIC_LLM_BACKENDS.md).

## Multi-language scans

Python is the default. To scan Go or TypeScript, install the optional
adapter:

```bash
./install.sh --with-go    # then: autofix scan --root /your/go/repo
./install.sh --with-jsts  # then: autofix scan --root /your/ts/repo
```

The Go adapter auto-downloads `scip-go` on first use. The TypeScript
adapter requires `scip-typescript` from npm:

```bash
npm install -g @sourcegraph/scip-typescript
```

Without these binaries, the language adapter falls back to a
Tree-sitter-only path (still works, less precise).

## Command reference

```bash
autofix scan          # scan the latest commit (default: HEAD~1..HEAD)
autofix watch         # long-running scanner (needs watchman)
autofix replay        # reproduce a past scan
autofix export-sarif  # write SARIF for a past scan
autofix policy        # inspect or validate .autofix/autofix-policy.json
autofix --help        # full flag reference
```

Every subcommand accepts `--help` for its flags.

## Cron / CI

Hourly cron:

```cron
0 * * * * cd /repo && /path/to/.venv/bin/autofix scan --root . \
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

## On-disk layout

```
<repo>/.autofix/
  events.jsonl                       # append-only event log (used for replay)
  autofix-policy.json                # your policy (optional)
  scans/<scan-id>/findings.sarif     # SARIF output
  state/index/                       # SCIP symbol/reference index
  state/embedding-sidecar/           # semantic dedup index (with --with-dedup)
```

You probably want to add `.autofix/scans/` and `.autofix/state/` to
your `.gitignore`. Keep `.autofix/autofix-policy.json` checked in.

## Requirements

- Python 3.11, 3.12, or 3.13
- `git` (the tool reads `git diff`)
- `claude` CLI **or** any OpenAI-compatible endpoint, for LLM explanations
- `watchman` daemon, only if you use `autofix watch`

## Documentation

- [`docs/AGENTIC_LLM_BACKENDS.md`](docs/AGENTIC_LLM_BACKENDS.md) — LLM
  backend setup (Claude, OpenAI, Ollama, vLLM)
- [`docs/AUTOFIX_STANDALONE.md`](docs/AUTOFIX_STANDALONE.md) — operations
  runbook (cron, daemon, log rotation)
- [`docs/architecture.md`](docs/architecture.md) — how it works
  internally (the 5-layer funnel, SCIP indexing, SARIF emission,
  replay determinism). Read this if you're contributing or curious.

## Contributing

```bash
./install.sh --dev --all
pytest tests/autofix/
```

Bug reports and PRs welcome at https://github.com/dynos-fit/autofix.

## License

MIT.
