import sys
from pathlib import Path

from benchmarks.agent_efficiency.instrumentation.core import apply_patch_specs, read_trace_events


def test_runtime_patch_records_trace(tmp_path: Path) -> None:
    module_path = tmp_path / "dummy_agent_module.py"
    module_path.write_text(
        "def call_model():\n"
        "    return {'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        import dummy_agent_module  # type: ignore

        trace_file = tmp_path / "trace.jsonl"
        handles = apply_patch_specs(
            [
                {
                    "target": "dummy_agent_module:call_model",
                    "name": "llm_call",
                    "event_type": "llm",
                    "usage_extractor": "generic_dict",
                }
            ],
            trace_file=trace_file,
        )
        try:
            result = dummy_agent_module.call_model()
        finally:
            for handle in reversed(handles):
                handle.restore()

        assert result["usage"]["total_tokens"] == 15
        events = read_trace_events(trace_file)
        assert len(events) == 1
        assert events[0]["name"] == "llm_call"
        assert events[0]["event_type"] == "llm"
        assert events[0]["usage"]["total_tokens"] == 15
    finally:
        sys.path.remove(str(tmp_path))
