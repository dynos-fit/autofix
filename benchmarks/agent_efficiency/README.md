# Agent Efficiency Benchmark

This folder contains a standalone benchmark harness for measuring both:

- coding-agent task performance
- token usage and efficiency

It is intentionally separate from the main `autofix` package so we can evolve benchmarking without entangling product code.

## Why this exists

There are strong coding benchmarks and strong agent-evaluation harnesses, but there is no single widely adopted benchmark that cleanly answers:

"How well does a coding agent solve tasks per token spent?"

This harness combines the strongest ideas from recent benchmark work:

- executable verification from SWE-bench-style evaluations
- realistic task execution from Terminal-Bench
- small messy repo tasks from CCBench
- token, latency, and rollout accounting inspired by HAL and SWE-Effi-style reporting

## What it measures

For every task run, the harness records:

- whether the task passed verification after the agent ran
- whether the fixture already passed before the run, which flags a bad benchmark task
- exact token usage when the agent exposes it
- estimated token usage when it does not
- wall-clock runtime
- files changed, insertions, and deletions
- scope violations against optional `allowed_files` and `forbidden_files`
- trace summaries including LLM-call count, tool-call count, and per-tool totals
- agent return code and captured stdout/stderr

At the suite level, it reports:

- success rate
- exact-token coverage
- average, median, and max total tokens
- token snowball index
- expensive failure ratio
- scope discipline
- mean steps
- solved tasks per 1k tokens
- solved tasks per minute
- resource AUC
- optional cost per resolved task when pricing is supplied

## Folder layout

- `runner.py`: main benchmark runner
- `metrics.py`: suite-level efficiency metrics
- `suite.json`: suite manifest
- `RESEARCH.md`: benchmark design notes and source links
- `tasks/`: benchmark tasks and tiny fixture repos
- `adapters/`: agent adapter examples, including a real `autofix` adapter
- `instrumentation/`: decorators, monkeypatch helpers, and a bootstrap runner for codebases you do not want to rewrite

## Benchmark protocol

Each task includes:

- a starter repo under `repo/`
- a human-readable instruction
- executable verification commands
- optional scope rules

The runner:

1. copies the task repo into a temp workspace
2. initializes a git baseline commit
3. runs verification before the agent to validate the fixture
4. writes the task prompt to `prompt.txt`
5. invokes the configured agent command
6. runs verification again after the agent finishes
7. computes diff, scope, trace, and efficiency metrics
8. writes per-task and suite-level JSON and Markdown reports

## Exact vs estimated tokens

Exact token accounting is preferred.

If your agent can write a JSON usage report, the runner will consume:

```json
{
  "prompt_tokens": 1234,
  "completion_tokens": 456,
  "total_tokens": 1690,
  "exact": true
}
```

If no usage file is produced, the runner falls back to a transparent heuristic estimate and marks the run as estimated.

## Scope rules

Tasks can define an optional `scope` block:

```json
{
  "scope": {
    "allowed_files": ["src/calc.py"],
    "forbidden_files": ["tests"]
  }
}
```

Any file touched outside `allowed_files`, or inside `forbidden_files`, is recorded as a scope violation and the run does not count as a clean success.

## Non-invasive instrumentation

For Python codebases, the benchmark can ingest per-action traces without forcing a large refactor.

There are two ways to do that:

1. Decorator path:
   wrap target functions with the tracing decorator from `instrumentation/core.py`
2. Runtime patch path:
   use the bootstrap runner plus a patch config to monkeypatch dotted targets at runtime

This means other teams can benchmark an existing agent loop without rewriting their whole codebase around this harness.

### Decorator example

```python
from benchmarks.agent_efficiency.instrumentation import traced

@traced(
    "llm_call",
    trace_file="trace.jsonl",
    usage_extractor="openai",
    event_type="llm",
)
def call_model(...):
    ...
```

### Runtime patch example

```bash
python -m benchmarks.agent_efficiency.instrumentation.bootstrap \
  --trace-file /tmp/trace.jsonl \
  --patch-config benchmarks/agent_efficiency/instrumentation/example_patch_config.json \
  --module my_agent.cli -- --root /path/to/repo
```

In that mode, the benchmark does not require you to rewrite the target module. You identify functions by dotted path and the bootstrap layer wraps them before execution.

## Included adapters

### Mock adapter

The mock agent is only for validating the harness itself.

```bash
python benchmarks/agent_efficiency/runner.py \
  --adapter-config benchmarks/agent_efficiency/adapters/mock_agent.json \
  --output-dir benchmarks/agent_efficiency/out/mock-run
```

### Zero-touch autofix adapter

`adapters/autofix_standalone.py` monkeypatches the real `autofix` seams at runtime:

- `autofix.llm_backend.run_prompt`
- `autofix.agent_loop._execute_action`

It then runs the real review loop followed by fix loops, writes an estimated `usage.json`, and emits a `trace.jsonl` with LLM and tool events. The product code stays untouched.

```bash
python benchmarks/agent_efficiency/runner.py \
  --adapter-config benchmarks/agent_efficiency/adapters/autofix_standalone.json \
  --output-dir benchmarks/agent_efficiency/out/autofix-run
```

## Running a real coding agent

Create an adapter config with a command template. The template can reference:

- `{repo_root}`
- `{python_executable}`
- `{task_id}`
- `{task_file}`
- `{workdir}`
- `{prompt_file}`
- `{usage_file}`
- `{trace_file}`

You can optionally include:

- `model_hint`: used for reporting and pricing lookup
- `pricing`: input/output USD-per-1M token rates

Example shape:

```json
{
  "name": "my-agent",
  "model_hint": "gpt-4o-mini",
  "pricing": {
    "gpt-4o-mini": {
      "input_per_million": 0.0,
      "output_per_million": 0.0
    }
  },
  "command_template": "my-agent --cwd {workdir} --task-file {task_file} --prompt-file {prompt_file} --usage-file {usage_file} --trace-file {trace_file}"
}
```

Then run:

```bash
python benchmarks/agent_efficiency/runner.py \
  --adapter-config path/to/adapter.json \
  --output-dir benchmarks/agent_efficiency/out/my-agent-run
```

## Scope

The included starter tasks are intentionally small. They are designed for local iteration and regression tracking, not as a replacement for external benchmarks like SWE-bench-Live or Terminal-Bench.

The harness is the main deliverable here. The starter suite is a seed set you can expand.
