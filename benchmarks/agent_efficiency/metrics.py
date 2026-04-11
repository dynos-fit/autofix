"""Suite-level metrics for coding-agent efficiency benchmarks."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, median
from typing import Any


PricingTable = dict[str, tuple[float, float]]


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def normalize_pricing(pricing: Any) -> PricingTable:
    """Normalize user-provided pricing data to USD-per-1M-token tuples."""

    table: PricingTable = {"default": (0.0, 0.0)}
    if not isinstance(pricing, dict):
        return table

    for key, value in pricing.items():
        name = str(key)
        if isinstance(value, (list, tuple)) and len(value) == 2:
            table[name] = (_safe_float(value[0]), _safe_float(value[1]))
            continue
        if isinstance(value, dict):
            table[name] = (
                _safe_float(value.get("input_per_million")),
                _safe_float(value.get("output_per_million")),
            )
    return table


def _match_pricing(model_hint: str, pricing: PricingTable) -> tuple[float, float]:
    if not model_hint:
        return pricing.get("default", (0.0, 0.0))
    lowered = model_hint.lower()
    for key, value in pricing.items():
        if key == "default":
            continue
        if key.lower() in lowered:
            return value
    return pricing.get("default", (0.0, 0.0))


def _estimate_cost(task_report: dict[str, Any], *, model_hint: str, pricing: PricingTable) -> float:
    prompt_tokens = _safe_int(task_report.get("token_usage", {}).get("prompt_tokens"))
    completion_tokens = _safe_int(task_report.get("token_usage", {}).get("completion_tokens"))
    input_rate, output_rate = _match_pricing(model_hint, pricing)
    return (prompt_tokens / 1_000_000) * input_rate + (completion_tokens / 1_000_000) * output_rate


def _resource_auc(task_reports: list[dict[str, Any]]) -> float:
    if not task_reports:
        return 0.0

    ordered = sorted(task_reports, key=lambda item: _safe_int(item.get("token_usage", {}).get("total_tokens")))
    total_tokens = sum(_safe_int(item.get("token_usage", {}).get("total_tokens")) for item in ordered) or 1
    total_resolved = sum(1 for item in ordered if bool(item.get("success"))) or 1

    previous_x = 0.0
    previous_y = 0.0
    auc = 0.0
    running_tokens = 0
    running_resolved = 0
    for item in ordered:
        running_tokens += _safe_int(item.get("token_usage", {}).get("total_tokens"))
        if bool(item.get("success")):
            running_resolved += 1
        x = running_tokens / total_tokens
        y = running_resolved / total_resolved
        auc += (x - previous_x) * (previous_y + y) / 2
        previous_x = x
        previous_y = y
    return max(0.0, min(1.0, auc))


@dataclass
class RunSummary:
    tasks_run: int
    tasks_solved: int
    success_rate: float
    invalid_fixtures: int
    exact_token_coverage: float
    avg_total_tokens: float
    median_total_tokens: float
    max_total_tokens: int
    token_snowball_index: float
    expensive_failure_ratio: float
    avg_wall_time_seconds: float
    solved_per_1k_tokens: float
    solved_per_minute: float
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    total_cost_usd: float
    cost_per_resolved_task: float
    scope_discipline: float
    mean_steps: float
    resource_auc: float
    tool_call_totals: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks_run": self.tasks_run,
            "tasks_solved": self.tasks_solved,
            "resolved": self.tasks_solved,
            "success_rate": self.success_rate,
            "pass_rate": self.success_rate,
            "invalid_fixtures": self.invalid_fixtures,
            "exact_token_coverage": self.exact_token_coverage,
            "avg_total_tokens": self.avg_total_tokens,
            "mean_tokens": self.avg_total_tokens,
            "median_total_tokens": self.median_total_tokens,
            "median_tokens": self.median_total_tokens,
            "max_total_tokens": self.max_total_tokens,
            "max_tokens": self.max_total_tokens,
            "token_snowball_index": self.token_snowball_index,
            "expensive_failure_ratio": self.expensive_failure_ratio,
            "avg_wall_time_seconds": self.avg_wall_time_seconds,
            "mean_wall_time_sec": self.avg_wall_time_seconds,
            "solved_per_1k_tokens": self.solved_per_1k_tokens,
            "token_efficiency_per_1k": self.solved_per_1k_tokens,
            "solved_per_minute": self.solved_per_minute,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_input_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_output_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "cost_per_resolved_task": self.cost_per_resolved_task,
            "scope_discipline": self.scope_discipline,
            "mean_steps": self.mean_steps,
            "resource_auc": self.resource_auc,
            "tool_call_totals": dict(self.tool_call_totals),
        }


def summarize_task_reports(
    task_reports: list[dict[str, Any]],
    *,
    model_hint: str = "",
    pricing: Any = None,
) -> RunSummary:
    normalized_pricing = normalize_pricing(pricing)
    total = len(task_reports)
    solved_reports = [item for item in task_reports if bool(item.get("success"))]
    failed_reports = [item for item in task_reports if not bool(item.get("success"))]
    total_tokens_list = [_safe_int(item.get("token_usage", {}).get("total_tokens")) for item in task_reports]
    wall_times = [_safe_float(item.get("wall_time_seconds")) for item in task_reports]
    steps = [_safe_int(item.get("steps")) for item in task_reports]
    exact_reports = [item for item in task_reports if bool(item.get("token_usage", {}).get("exact"))]
    invalid_fixtures = sum(1 for item in task_reports if bool(item.get("tests_passed_before")))
    scope_clean = sum(1 for item in task_reports if not list(item.get("scope_violations") or []))

    total_prompt_tokens = sum(_safe_int(item.get("token_usage", {}).get("prompt_tokens")) for item in task_reports)
    total_completion_tokens = sum(_safe_int(item.get("token_usage", {}).get("completion_tokens")) for item in task_reports)
    total_tokens = total_prompt_tokens + total_completion_tokens

    median_tokens = float(median(total_tokens_list)) if total_tokens_list else 0.0
    max_tokens = max(total_tokens_list) if total_tokens_list else 0
    mean_success_tokens = (
        mean(_safe_int(item.get("token_usage", {}).get("total_tokens")) for item in solved_reports)
        if solved_reports
        else 0.0
    )
    mean_failure_tokens = (
        mean(_safe_int(item.get("token_usage", {}).get("total_tokens")) for item in failed_reports)
        if failed_reports
        else 0.0
    )
    expensive_failure_ratio = (mean_failure_tokens / mean_success_tokens) if mean_success_tokens else 0.0
    token_snowball_index = (max_tokens / median_tokens) if median_tokens else 0.0
    solved = len(solved_reports)
    total_wall = sum(wall_times)
    total_cost = sum(_estimate_cost(item, model_hint=model_hint, pricing=normalized_pricing) for item in task_reports)

    tool_call_totals: dict[str, int] = {}
    for item in task_reports:
        trace = item.get("trace", {})
        totals = trace.get("tool_call_totals", {})
        if not isinstance(totals, dict):
            continue
        for name, count in totals.items():
            key = str(name)
            tool_call_totals[key] = tool_call_totals.get(key, 0) + _safe_int(count)

    return RunSummary(
        tasks_run=total,
        tasks_solved=solved,
        success_rate=round(solved / total, 4) if total else 0.0,
        invalid_fixtures=invalid_fixtures,
        exact_token_coverage=round(len(exact_reports) / total, 4) if total else 0.0,
        avg_total_tokens=round(sum(total_tokens_list) / total, 2) if total else 0.0,
        median_total_tokens=median_tokens,
        max_total_tokens=max_tokens,
        token_snowball_index=round(token_snowball_index, 4),
        expensive_failure_ratio=round(expensive_failure_ratio, 4),
        avg_wall_time_seconds=round(total_wall / total, 3) if total else 0.0,
        solved_per_1k_tokens=round(solved / (total_tokens / 1000), 4) if total_tokens else 0.0,
        solved_per_minute=round(solved / (total_wall / 60), 4) if total_wall else 0.0,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
        total_tokens=total_tokens,
        total_cost_usd=round(total_cost, 6),
        cost_per_resolved_task=round(total_cost / solved, 6) if solved else 0.0,
        scope_discipline=round(scope_clean / total, 4) if total else 1.0,
        mean_steps=round(mean(steps), 3) if steps else 0.0,
        resource_auc=round(_resource_auc(task_reports), 4),
        tool_call_totals=tool_call_totals,
    )
