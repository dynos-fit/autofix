# autofix

Standalone repository scanner and repair runner.

`autofix` scans a target git repository, turns findings into structured actions, and uses the Dynos repair pipeline as its execution backend for safe fixes.

## Run

```bash
python3 -m autofix scan --root /path/to/target-repo
```

Or:

```bash
bin/autofix scan --root /path/to/target-repo
```

For a local wrapper install:

```bash
./install.sh
```

## Cron

Example hourly run:

```cron
0 * * * * cd /home/hassam/autofix-standalone && /home/hassam/autofix-standalone/bin/autofix scan --root /path/to/target-repo >> /var/log/autofix.log 2>&1
```

For safe debugging without opening real issues or PRs:

```cron
0 * * * * cd /home/hassam/autofix-standalone && /home/hassam/autofix-standalone/bin/autofix scan --root /path/to/target-repo --dry-run >> /var/log/autofix.log 2>&1
```

Top-level `.autofix/` keeps control files like `autofix-policy.json`, `events.jsonl`, `scan.lock`, `state/`, and `scans/`.
The current aggregate state lives under `.autofix/state/current/`, historical state snapshots live under `.autofix/state/history/<scan-id>/`, and per-scan execution artifacts live under `.autofix/scans/<scan-id>/`.

## Model

The workflow is:

1. scan the target repo
2. detect findings
3. route each finding by policy and risk
4. for safe findings, invoke the Dynos pipeline through shell commands
5. verify changes
6. open PRs or issues

## Requirements

- `python3`
- `git`
- `gh` for issues and PRs
- `claude` for autonomous fixes

## Agentic LLM Backends

The scanner now supports two LLM backends through repo-local config:

- `claude_cli`: existing behavior using the `claude` CLI
- `openai_compatible`: any OpenAI-style chat endpoint, including Ollama and `llama.cpp`

Example local-model config:

```bash
python3 -m autofix config set --root /path/to/repo llm_backend openai_compatible
python3 -m autofix config set --root /path/to/repo llm_base_url http://127.0.0.1:11434/v1
python3 -m autofix config set --root /path/to/repo llm_api_key ollama
python3 -m autofix config set --root /path/to/repo review_model qwen2.5-coder:7b-16k
python3 -m autofix config set --root /path/to/repo fix_model qwen2.5-coder:7b-16k
python3 -m autofix config set --root /path/to/repo llm_max_steps 12
python3 -m autofix config set --root /path/to/repo review_chunk_lines 80
python3 -m autofix config set --root /path/to/repo review_file_truncation 160
python3 -m autofix config set --root /path/to/repo fix_surrounding_lines 6
python3 -m autofix config set --root /path/to/repo fix_neighbor_files 1
python3 -m autofix config set --root /path/to/repo fix_neighbor_lines 24
```

`review_model` drives review prompts. `fix_model` drives autofix repair work. `llm_max_steps` bounds the low-token agent loop used by `openai_compatible` backends.

Behavior by backend:

- `claude_cli`: keeps the original large-context Claude repair flow
- `openai_compatible`: uses an on-demand review agent and a bounded local fix agent so the model pulls context incrementally instead of receiving one giant prompt

See [`docs/AGENTIC_LLM_BACKENDS.md`](/Users/hassam/Documents/autofix-standalone/docs/AGENTIC_LLM_BACKENDS.md) for the design and maintenance notes.
The agent system prompts live in [`autofix/llm_io/prompts/`](/Users/hassam/Documents/autofix-standalone/autofix/llm_io/prompts).

## Benchmarking

The agent benchmark harness lives under [`benchmarks/agent_efficiency/`](/Users/hassam/Documents/autofix-standalone/benchmarks/agent_efficiency).

It can benchmark:

- the real `autofix` review and fix loops through a zero-touch adapter
- external agent loops through decorators or runtime monkeypatching

See [`benchmarks/agent_efficiency/README.md`](/Users/hassam/Documents/autofix-standalone/benchmarks/agent_efficiency/README.md) for setup, metrics, and task format details.

There is also a source-instrumented `agent-bench` integration under [`benchmarks/agent_bench/`](/Users/hassam/Documents/autofix-standalone/benchmarks/agent_bench), which decorates the real `autofix` seams instead of monkey patching them at runtime.
## Operations

See [`docs/AUTOFIX_STANDALONE.md`](/home/hassam/autofix-standalone/docs/AUTOFIX_STANDALONE.md).
