from autofix.llm_backend import build_claude_prompt_command


def test_build_claude_prompt_command_omits_model_for_default() -> None:
    assert build_claude_prompt_command("hello", model="default") == ["claude", "-p", "hello"]


def test_build_claude_prompt_command_uses_configured_model() -> None:
    assert build_claude_prompt_command("hello", model="qwen2.5-coder:7b") == [
        "claude",
        "-p",
        "hello",
        "--model",
        "qwen2.5-coder:7b",
    ]
