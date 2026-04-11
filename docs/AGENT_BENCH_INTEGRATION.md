# Agent-Bench Integration Report

## Executive Summary

Yes, this repo can integrate `agent-bench` without monkey patching `autofix`.

The clean path is:

1. add `agent-bench` as a benchmark/dev dependency instead of keeping a parallel local harness
2. instrument the real `autofix` seams in source with `agent-bench` decorators or manual record hooks
3. run `agent-bench` through its Python API from a local runner script in this repo
4. migrate the current `benchmarks/agent_efficiency/tasks/*` corpus into `agent-bench` fixtures

The main caveat is token accounting: today, `agent-bench`'s decorator/manual APIs are estimate-based, while exact SDK usage is only wired through its runtime patch path. If exact tokens matter for the non-monkey-patch integration, `agent-bench` needs a small upstream extension.

## Current State

This repo already has a custom benchmark harness under `benchmarks/agent_efficiency/`.

Relevant facts:

- `benchmarks/agent_efficiency/adapters/autofix_standalone.py` benchmarks the real loops by monkey patching `autofix.llm_backend.run_prompt` and `autofix.agent_loop._execute_action`.
- `benchmarks/agent_efficiency/README.md` explicitly documents runtime patching as one of the supported modes.
- `autofix/agent_loop.py` already has two obvious instrumentation seams:
  - `run_prompt(...)` for every model call
  - `_execute_action(...)` for every tool dispatch
- `autofix/llm_backend.py` currently returns `LLMResult(returncode, stdout, stderr)` and drops any provider usage metadata.

Upstream `agent-bench` already provides what this repo needs structurally:

- `@trace_llm`
- `@trace_tool`
- `BenchmarkSession`
- `FixtureRunner`
- fixture verification and scope checks

So the missing piece is not capability. The missing piece is integration shape.

## Recommended Integration

### 1. Use `agent-bench` as the benchmark engine

Do not keep evolving `benchmarks/agent_efficiency` as a second benchmark implementation.

Preferred setup:

- keep `agent-bench` as a separate package/repo
- add it as a dev-only dependency for this repo
- put only `autofix`-specific glue in this repo

Why:

- the custom harness here duplicates `agent-bench` runner, metrics, verification, reporting, and patching logic
- keeping both will split fixes and benchmark semantics
- `agent-bench` is already generic and package-shaped

### 2. Instrument source seams instead of patching them at runtime

The minimal no-monkey-patch integration is to instrument the real `autofix` seams in code.

#### LLM seam

Instrument `autofix.llm_backend.run_prompt`.

That function is the single model boundary for both `run_agent_loop(...)` and `run_review_agent_loop(...)`, so one instrumentation point covers both loops.

Example shape:

```python
from agent_bench import trace_llm

@trace_llm
def run_prompt(...):
    ...
```

This is the smallest viable integration.

#### Tool seam

Instrument the tool dispatcher in `autofix.agent_loop.py`.

Technically, `@trace_tool` can be placed directly on `_execute_action(...)`. That would work and would already remove monkey patching.

But the better integration is to first promote that private helper into a stable seam, for example:

- `execute_action(...)`
- or `ToolExecutor.execute(...)`

Then instrument that stable public seam.

Why this small refactor is worth it:

- `_execute_action` is private and benchmark code should not depend on private names long-term
- a public seam is easier to test and reason about
- future benchmark integrations will not need to know internal function names

Example shape:

```python
from agent_bench import trace_tool

@trace_tool
def execute_action(action: dict, *, root: Path, subprocess_module) -> str:
    ...
```

### 3. Run `agent-bench` via Python API, not its CLI

This matters because upstream `agent-bench` CLI currently hardcodes adapter imports as:

`agent_bench.adapters.<name>`

That is fine for built-in adapters, but awkward for a local adapter living inside this repo.

So the clean integration is:

- create a local runner such as `benchmarks/agent_bench/run_autofix_benchmark.py`
- import `FixtureRunner` and `RunnerConfig` directly
- import a local `autofix` agent callable from this repo

That avoids:

- vendoring an adapter into the `agent_bench` package
- changing `sys.path`
- reintroducing monkey-patch-style indirection

### 4. Keep the adapter thin

The local adapter should only translate `agent-bench` fixtures into calls into the real `autofix` loops.

Recommended local files:

- `benchmarks/agent_bench/autofix_adapter.py`
- `benchmarks/agent_bench/run_autofix_benchmark.py`

The adapter should:

- accept `(workdir: Path, fixture: Fixture)`
- build the review prompt from `fixture.description`, scope, and verification info
- call `run_review_agent_loop(...)`
- convert review findings into one or more fix prompts
- call `run_agent_loop(...)`
- leave all benchmarking, verification, and reporting to `agent-bench`

That keeps benchmark ownership separated:

- `autofix` owns agent behavior
- `agent-bench` owns evaluation behavior

### 5. Migrate tasks to `agent-bench` fixture format

The current local tasks are close, but they are not drop-in compatible.

Current local shape:

- `benchmarks/agent_efficiency/tasks/<task>/task.json`
- starter repo under `repo/`
- verification commands as shell strings with formatting placeholders

`agent-bench` expects:

- one directory per fixture
- `fixture.json`
- starter repo under `bugged/`
- `test_command` as `list[str]`, not a shell string

Required conversion mapping:

- `task.json.id` -> `fixture.json.id`
- `title` -> `name`
- `instruction` -> `description`
- `category`, `difficulty` -> same keys
- `scope.allowed_files`, `scope.forbidden_files` -> same structure
- `repo/` -> `bugged/`
- `verification[0].command` -> `test_command` argv list

Important incompatibility:

`agent-bench` verifier runs `subprocess.run(command_list, ...)`, so commands like:

```text
{python_executable} -m pytest -q
```

must be normalized to:

```json
["python3", "-m", "pytest", "-q"]
```

before the fixtures will run cleanly.

## Exact Token Accounting

This is the one place where the no-monkey-patch path is not feature-parity with the current patched path.

### What works today

If you use `@trace_llm` or `record_llm_call`, `agent-bench` will record token counts, but they are estimates based on `tiktoken` or `char/4`.

That is good enough for:

- pass/fail benchmarking
- relative efficiency comparisons
- step/tool/wall-time reporting

### What does not work cleanly today

Upstream `agent-bench` only supports SDK usage overrides in the runtime patch path (`traced(..., usage_extractor=...)`).

The decorator/manual path does not currently expose a public way to say:

- "use these exact prompt/completion token counts from the provider response"

### Recommended upstream extension

Before relying on `agent-bench` for exact-token reporting, add one small upstream capability:

- `trace_llm(..., usage_from=...)`
- or `record_llm_call(..., prompt_tokens=..., completion_tokens=..., tokenizer="sdk:openai")`

With that in place, `autofix` can stay fully non-monkey-patched and still emit exact usage whenever the backend exposes it.

### Repo-side prerequisite

`autofix.llm_backend.LLMResult` should carry usage metadata when available.

Recommended direction:

```python
@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    exact: bool = False
    tokenizer: str = ""


@dataclass(frozen=True)
class LLMResult:
    returncode: int
    stdout: str
    stderr: str = ""
    usage: LLMUsage | None = None
```

That change is useful even outside benchmarking because it preserves provider metadata instead of discarding it.

## Minimal Refactor Plan

### Phase 1: Source instrumentation

1. Add `agent-bench` as a dev dependency.
2. Instrument the LLM seam in `autofix.llm_backend.run_prompt`.
3. Promote `_execute_action(...)` to a stable seam and instrument it with `@trace_tool`.
4. Run the existing `autofix` test suite to confirm no behavior change.

### Phase 2: Local runner integration

1. Add `benchmarks/agent_bench/autofix_adapter.py`.
2. Add `benchmarks/agent_bench/run_autofix_benchmark.py`.
3. Run one migrated fixture end-to-end through `FixtureRunner`.

### Phase 3: Fixture migration

1. Convert one existing task from `benchmarks/agent_efficiency/tasks/`.
2. Prove report parity on:
   - pass/fail
   - files touched
   - scope violations
   - tool counts
   - token estimates
3. Convert the rest of the suite.

### Phase 4: Remove duplicate harness

Once the migrated suite is stable:

- deprecate `benchmarks/agent_efficiency/`
- remove the patched `autofix_standalone.py` adapter
- keep only thin `autofix` integration code plus `agent-bench` fixtures

## Recommended File Layout In This Repo

```text
docs/
  AGENT_BENCH_INTEGRATION.md
benchmarks/
  agent_bench/
    autofix_adapter.py
    run_autofix_benchmark.py
    fixtures/
      python_small_autofix/
        <fixture>/
          fixture.json
          bugged/
```

## Risks And Open Questions

1. `agent-bench` CLI adapter loading is package-local today.
   Use the Python API locally unless upstream adds fully-qualified adapter imports or plugin entry points.

2. Exact token reporting is incomplete for the non-patch path.
   If exact-token benchmarking is a hard requirement, upstream needs the small extension described above.

3. `claude_cli` may still not provide exact usage.
   Even after the refactor, exact tokens depend on whether the underlying provider surface returns usage data.

4. Benchmarking a private seam is workable but not ideal.
   If `_execute_action(...)` stays private, the integration will still be code-level and non-monkey-patched, but the benchmark will depend on an internal symbol.

## Bottom Line

The recommended integration is:

- do not port more of the local custom harness forward
- instrument `autofix` in source at the real LLM and tool seams
- run upstream `agent-bench` from a local Python runner
- migrate the current benchmark tasks into `agent-bench` fixtures

That gives this repo a clean, non-monkey-patched integration with `agent-bench`, while keeping the benchmark engine generic and keeping `autofix` changes small and maintainable.
