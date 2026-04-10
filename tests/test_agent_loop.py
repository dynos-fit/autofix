from pathlib import Path

from autofix.agent_loop import _execute_action, _parse_action


def test_parse_action_strips_markdown_fence() -> None:
    action = _parse_action('```json\n{"action":"finish","summary":"done"}\n```')
    assert action["action"] == "finish"


def test_execute_replace_text_updates_file(tmp_path: Path) -> None:
    path = tmp_path / "example.py"
    path.write_text("value = 1\n", encoding="utf-8")
    result = _execute_action(
        {"action": "replace_text", "path": "example.py", "old": "1", "new": "2"},
        root=tmp_path,
        subprocess_module=__import__("subprocess"),
    )
    assert '"ok": true' in result.lower()
    assert path.read_text(encoding="utf-8") == "value = 2\n"


def test_parse_finish_review_action() -> None:
    action = _parse_action('{"action":"finish_review","findings":[]}')
    assert action["action"] == "finish_review"
