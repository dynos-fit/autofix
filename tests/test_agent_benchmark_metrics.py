from benchmarks.agent_efficiency.metrics import summarize_task_reports


def test_summarize_task_reports_includes_efficiency_and_scope_metrics() -> None:
    reports = [
        {
            "success": True,
            "tests_passed_before": False,
            "steps": 2,
            "wall_time_seconds": 10.0,
            "scope_violations": [],
            "token_usage": {
                "prompt_tokens": 60,
                "completion_tokens": 40,
                "total_tokens": 100,
                "exact": True,
            },
            "trace": {
                "tool_call_totals": {
                    "read_file": 2,
                    "write_file": 1,
                }
            },
        },
        {
            "success": False,
            "tests_passed_before": True,
            "steps": 4,
            "wall_time_seconds": 20.0,
            "scope_violations": ["modified forbidden path: tests/test_calc.py"],
            "token_usage": {
                "prompt_tokens": 240,
                "completion_tokens": 160,
                "total_tokens": 400,
                "exact": False,
            },
            "trace": {
                "tool_call_totals": {
                    "read_file": 5,
                }
            },
        },
    ]

    summary = summarize_task_reports(
        reports,
        model_hint="test-model",
        pricing={
            "test-model": {
                "input_per_million": 1.0,
                "output_per_million": 2.0,
            }
        },
    )

    assert summary.tasks_run == 2
    assert summary.tasks_solved == 1
    assert summary.success_rate == 0.5
    assert summary.invalid_fixtures == 1
    assert summary.exact_token_coverage == 0.5
    assert summary.avg_total_tokens == 250.0
    assert summary.median_total_tokens == 250.0
    assert summary.max_total_tokens == 400
    assert summary.token_snowball_index == 1.6
    assert summary.expensive_failure_ratio == 4.0
    assert summary.scope_discipline == 0.5
    assert summary.mean_steps == 3.0
    assert summary.resource_auc == 0.9
    assert summary.solved_per_1k_tokens == 2.0
    assert summary.solved_per_minute == 2.0
    assert summary.total_cost_usd == 0.0007
    assert summary.cost_per_resolved_task == 0.0007
    assert summary.tool_call_totals == {
        "read_file": 7,
        "write_file": 1,
    }
