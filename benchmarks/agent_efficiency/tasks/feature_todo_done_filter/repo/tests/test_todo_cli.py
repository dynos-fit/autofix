from src.todo_cli import list_titles


TASKS = [
    {"title": "write docs", "done": True},
    {"title": "ship feature", "done": False},
    {"title": "fix bug", "done": True},
]


def test_list_titles_default_behavior():
    assert list_titles(TASKS) == ["write docs", "ship feature", "fix bug"]


def test_list_titles_done_only():
    assert list_titles(TASKS, done_only=True) == ["write docs", "fix bug"]
