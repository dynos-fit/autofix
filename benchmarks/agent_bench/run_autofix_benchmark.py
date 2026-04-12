#!/usr/bin/env python3
"""Run the real autofix loops against agent-bench fixtures.

Can be invoked two ways:
    python -m benchmarks.agent_bench.run_autofix_benchmark [flags]
    python benchmarks/agent_bench/run_autofix_benchmark.py [flags]
"""

from __future__ import annotations

import argparse
import importlib
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Ensure the repo root is on sys.path so both `benchmarks.*` and `autofix.*`
# imports work regardless of how this script is invoked (direct or via -m).
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.agent_bench.tasks_to_fixtures import materialize_agent_bench_fixtures

DEFAULT_TASKS_ROOT = _REPO_ROOT / "benchmarks" / "agent_bench" / "tasks"
DEFAULT_OUTPUT_ROOT = _REPO_ROOT / "benchmarks" / "agent_bench" / "out"
DEFAULT_SIBLING_AGENT_BENCH = _REPO_ROOT.parent / "agent-bench"


def _ensure_agent_bench_importable(agent_bench_root: str) -> None:
    try:
        importlib.import_module("agent_bench")
        return
    except ModuleNotFoundError:
        pass

    candidate = Path(agent_bench_root).resolve() if agent_bench_root else DEFAULT_SIBLING_AGENT_BENCH.resolve()
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

    try:
        importlib.import_module("agent_bench")
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "agent_bench is not importable. Install the package or pass "
            f"--agent-bench-root (tried {candidate})."
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_autofix_benchmark")
    parser.add_argument("--agent-bench-root", default="", help="Path to a local agent-bench checkout")
    parser.add_argument("--tasks-root", default=str(DEFAULT_TASKS_ROOT))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--only", default="", help="Comma-separated task ids")
    parser.add_argument("--backend", default="claude_cli")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--test-timeout", type=int, default=120)
    parser.add_argument("--keep-workdirs", action="store_true")
    return parser


def _output_dir(value: str) -> Path:
    if value:
        return Path(value)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_ROOT / f"autofix-{stamp}"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _ensure_agent_bench_importable(args.agent_bench_root)

    from agent_bench.metrics import DEFAULT_PRICING, summarize
    from agent_bench.reporter import write_report
    from agent_bench.runner import FixtureRunner, RunnerConfig
    from benchmarks.agent_bench.autofix_adapter import build_agent

    only = [item.strip() for item in args.only.split(",") if item.strip()] or None
    output_dir = _output_dir(args.output_dir)

    with tempfile.TemporaryDirectory(prefix="autofix_agent_bench_") as fixture_tmp:
        fixtures_dir = Path(fixture_tmp)
        materialize_agent_bench_fixtures(
            Path(args.tasks_root),
            fixtures_dir,
            only=only,
        )

        agent = build_agent(
            model=args.model or None,
            max_steps=args.max_steps,
            timeout=args.timeout,
            backend=args.backend,
            base_url=args.base_url,
            api_key=args.api_key,
        )
        runner = FixtureRunner(
            agent=agent,
            fixtures_dir=fixtures_dir,
            config=RunnerConfig(
                agent_timeout_sec=args.timeout,
                test_timeout_sec=args.test_timeout,
                keep_workdirs=args.keep_workdirs,
                model_hint=args.model or "",
            ),
        )
        results = runner.run_all(only=only)
        summary = summarize(results, pricing=DEFAULT_PRICING)
        run_json, summary_md = write_report(
            output_dir,
            results,
            summary,
            config={
                "backend": args.backend,
                "model": args.model,
                "max_steps": args.max_steps,
                "timeout": args.timeout,
                "test_timeout": args.test_timeout,
                "tasks_root": str(Path(args.tasks_root).resolve()),
                "only": args.only,
            },
        )

    print(f"pass rate: {summary.pass_rate*100:.1f}% ({summary.resolved}/{summary.total_tasks})")
    print(f"total tokens: {summary.total_tokens:,}")
    print(f"report: {run_json}")
    print(f"summary: {summary_md}")
    return 0 if summary.resolved == summary.total_tasks else 1


if __name__ == "__main__":
    raise SystemExit(main())
