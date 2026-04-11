# Autofix + agent-bench

This folder contains the non-monkey-patch integration with `agent-bench`.

What it does:

- decorates the real `autofix` LLM and tool seams in source
- reuses the existing local task corpus under `benchmarks/agent_efficiency/tasks/`
- materializes those tasks into temporary `agent-bench` fixtures at runtime
- runs the real review/fix loops through `agent-bench`'s `FixtureRunner`

## Prerequisite

`agent_bench` must be importable either because:

- it is installed in the current Python environment, or
- you have a local checkout and pass `--agent-bench-root`

The local runner will also auto-try a sibling checkout at `../agent-bench`.

## Run

```bash
python -m benchmarks.agent_bench.run_autofix_benchmark \
  --agent-bench-root ../agent-bench \
  --backend openai_compatible \
  --base-url http://127.0.0.1:11434/v1 \
  --api-key ollama \
  --model qwen2.5-coder:7b-16k \
  --only bugfix_take_limit
```

Reports are written under `benchmarks/agent_bench/out/` by default.
