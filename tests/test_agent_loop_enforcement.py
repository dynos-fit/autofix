import json
from pathlib import Path

import autofix.agent_loop as agent_loop
from autofix.llm_backend import LLMBackendConfig, LLMResult


class _FakeSubprocess:
    pass


def test_fix_loop_requires_inspection_before_finish(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    responses = iter(
        [
            LLMResult(returncode=0, stdout='{"action":"finish","summary":"done"}'),
            LLMResult(returncode=0, stdout='{"action":"list_files","path":"."}'),
            LLMResult(returncode=0, stdout='{"action":"finish","summary":"done"}'),
        ]
    )

    def fake_run_prompt(prompt: str, **kwargs) -> LLMResult:
        del kwargs
        calls.append(prompt)
        return next(responses)

    monkeypatch.setattr(agent_loop, "run_prompt", fake_run_prompt)

    result = agent_loop.run_agent_loop(
        root=tmp_path,
        task_prompt="Fix the bug.",
        model="default",
        backend_config=LLMBackendConfig(),
        max_steps=3,
        subprocess_module=_FakeSubprocess(),
        timeout=30,
    )

    assert result.ok is True
    assert result.steps == 3
    assert len(calls) == 3
    assert "inspect the repository before finishing" in calls[1].lower()


def test_review_loop_requires_inspection_before_finish_review(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    responses = iter(
        [
            LLMResult(returncode=0, stdout='{"action":"finish_review","findings":[]}'),
            LLMResult(returncode=0, stdout='{"action":"list_files","path":"."}'),
            LLMResult(returncode=0, stdout='{"action":"finish_review","findings":[]}'),
        ]
    )

    def fake_run_prompt(prompt: str, **kwargs) -> LLMResult:
        del kwargs
        calls.append(prompt)
        return next(responses)

    monkeypatch.setattr(agent_loop, "run_prompt", fake_run_prompt)

    result = agent_loop.run_review_agent_loop(
        root=tmp_path,
        task_prompt="Review the repo.",
        model="default",
        backend_config=LLMBackendConfig(),
        max_steps=3,
        subprocess_module=_FakeSubprocess(),
        timeout=30,
    )

    assert result.ok is True
    assert result.steps == 3
    assert len(calls) == 3
    assert "inspect the repository before finishing" in calls[1].lower()
