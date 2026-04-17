# `docs/rewrite/` — autofix rewrite docset

- Source task: `task-20260417-001`
- Source report: `deep-research-report.md`
- Date: 2026-04-17

## Purpose

This docset is the single source of truth for the multi-week rewrite of the autofix scanner from its current crawler-driven loop to an event-driven, incremental program-analysis system whose LLM is called only on compact evidence packets at the end of a deterministic funnel. The three sibling documents translate the architecture in `deep-research-report.md` into a repo-local gap analysis, a target-state design bounded by the locked surfaces, and a sequenced roadmap that future `/dynos-work:start` operators execute one task at a time.

## Table of contents

1. [gap-analysis.md](gap-analysis.md) — side-by-side mapping of the current `autofix/*.py` tree onto the 23 target-architecture rows from the research report, with every row marked keep-as-is / wrap / replace / new and every MISSING row paired with a proposed module path.
2. [target-architecture.md](target-architecture.md) — the new core scan loop expressed as module boundaries, JSON data schemas, a 5-layer reference diagram, an end-to-end scan sequence, a language-registry `Protocol` with Python / JS-TS / Go adapters, and a clean-slate plus deprecated CLI surface.
3. [roadmap.md](roadmap.md) — twelve sequenced `/dynos-work:start` tasks (Prototype through Production) with `Depends-on` DAG, Critical path, and Parallelizable lanes.

## Intended reader

The primary reader is a senior engineer planning a multi-week rewrite of a production scanner; they already know the current `autofix` codebase and the research report separately and need the mapping between them plus a safe execution order. The secondary readers are `/dynos-work:start` operators picking roadmap items one at a time; they have full repo access but shallow context on the overall plan.

Two reading modes are supported:

- **Strategic one-time read** — desktop, 30–60 minutes, top-to-bottom across all three sibling files, performed when the rewrite initiative kicks off. The gap analysis grounds the claims; the target-architecture document freezes the contracts; the roadmap sequences the work.
- **Tactical per-roadmap-item re-read** — 5–10 minutes per task invocation. The operator opens `roadmap.md`, finds their task block, follows the `Touches gap rows` and `Touches target-architecture sections` links to the specific anchors they need, and proceeds. No other cross-reading is required before `/dynos-work:start`.

## Locked surfaces

The non-goal paths below are locked by `.dynos/task-20260417-001/discovery-notes.md` and must not be edited by any task spawned from this roadmap. The new core loop wraps them through documented call sites (see [target-architecture.md#integration-with-locked-surfaces](target-architecture.md#integration-with-locked-surfaces)).

- `autofix/llm_io/**`
- `autofix/agent_loop.py`
- `autofix/llm_backend.py`
- `.autofix/state/**`
- `.autofix/autofix-policy.json`
- `.autofix/events.jsonl` schema
- `benchmarks/agent_bench/**`
