from pathlib import Path

from autofix.platform import persistent_project_dir, runtime_state_dir
from autofix.runtime import dynos


def test_runtime_dirs_are_local(tmp_path: Path) -> None:
    assert runtime_state_dir(tmp_path) == tmp_path / ".autofix"
    assert persistent_project_dir(tmp_path) == tmp_path / ".autofix"


def test_qtable_round_trip(tmp_path: Path) -> None:
    table = dynos.load_autofix_q_table(tmp_path)
    state = dynos.encode_autofix_state("syntax-error", ".py", "medium", "high")
    dynos.update_q_value(table, state, "attempt_fix", 0.8, None)
    dynos.save_autofix_q_table(tmp_path, table)

    loaded = dynos.load_autofix_q_table(tmp_path)
    assert loaded["entries"][state]["attempt_fix"] > 0


def test_template_round_trip(tmp_path: Path) -> None:
    finding = {"category": "dead-code", "evidence": {"file": "hooks/example.py"}}
    dynos.save_fix_template(tmp_path, finding, "--- a\n+++ b\n")
    match = dynos.find_matching_template(tmp_path, finding)
    assert match is not None
    assert "diff" in match
