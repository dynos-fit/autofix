import json

from autofix.llm_backend import LLMBackendConfig, _extract_message_content


def test_extract_message_content_from_string() -> None:
    payload = {"choices": [{"message": {"content": "hello"}}]}
    assert _extract_message_content(payload) == "hello"


def test_extract_message_content_from_text_blocks() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "tool_call", "id": "ignored"},
                        {"type": "text", "text": "world"},
                    ]
                }
            }
        ]
    }
    assert _extract_message_content(payload) == "hello\nworld"


def test_backend_config_defaults() -> None:
    cfg = LLMBackendConfig()
    assert json.loads(json.dumps(cfg.__dict__)) == {
        "backend": "claude_cli",
        "base_url": "",
        "api_key": "",
    }
