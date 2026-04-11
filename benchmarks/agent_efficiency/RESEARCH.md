# Research Notes

This harness was designed after reviewing existing agent and coding benchmarks.

## What exists today

### SWE-bench-Live

SWE-bench-Live is strong on:

- executable issue-resolution tasks
- recency and contamination resistance
- reproducible environments

It is a strong reference for "did the patch actually solve the task?"

Reference:
- [SWE-bench Goes Live!](https://arxiv.org/abs/2505.23419)

### Terminal-Bench 2.0

Terminal-Bench is strong on:

- realistic terminal workflows
- hard long-horizon tasks
- human-written solutions and comprehensive verification

It is a strong reference for evaluating tool-using agents in a CLI environment.

Reference:
- [Terminal-Bench: Benchmarking Agents on Hard, Realistic Tasks in Command Line Interfaces](https://arxiv.org/abs/2601.11868)

### CCBench

CCBench is strong on:

- small real-world codebases
- non-curated code that resembles private repos
- task verification through official tests

It is a strong reference for feature work on compact repos where contamination matters.

Reference:
- [CCBench](https://ccbench.org/)

### HAL

HAL is strong on:

- evaluation harness design
- large-scale rollout logging
- cost and token accounting
- cross-benchmark analysis

It is the clearest reference for why agent evaluation should include logs, costs, and operational traces instead of only final scores.

Reference:
- [Holistic Agent Leaderboard: The Missing Infrastructure for AI Agent Evaluation](https://arxiv.org/abs/2510.11977)

## What seems missing

There is still no widely adopted benchmark focused specifically on:

- coding-agent correctness
- token usage
- latency
- patch efficiency

in one lightweight local harness.

That gap is what this folder tries to address.

## Design decisions taken here

1. Executable verification is mandatory.
   Reason:
   final answers are less trustworthy than tests.

2. Token accounting is first-class.
   Reason:
   for coding agents, token cost is part of product quality.

3. Exact tokens are preferred, but estimates are allowed.
   Reason:
   many agent CLIs still do not expose usage in a standardized way.

4. Small starter tasks live inside the repo.
   Reason:
   we need a benchmark we can run locally and extend immediately.

5. The harness is adapter-based.
   Reason:
   the same benchmark should be usable with Codex, Claude Code, local agents, or wrapper scripts.

6. Instrumentation should be non-invasive when possible.
   Reason:
   a useful benchmark for external teams cannot require large application rewrites just to measure performance.

## Practical implication

This harness supports two instrumentation styles:

- direct decorators for teams who own the code
- runtime monkeypatch/bootstrap wrappers for teams who only want to point the benchmark at existing Python entrypoints

That is the compromise between observability quality and integration friction.
