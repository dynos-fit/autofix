"""Tests for autofix/cli/policy_command.py (NEW production module).

Covers:
- AC 9:  invoking with no flag exits 2
- AC 10: --show emits sorted JSON; missing file -> exit 1 with stderr substring
         "autofix-policy.json not found"
- AC 11: --validate parametrized: ok / wrong-type llm_tiered / wrong-type
         state_migration / non-int min_priority / index not dict / non-dict
         top-level / unparseable JSON. Each failure case asserts exit 2 and
         stderr substring matches "policy.<key>:" or "does not parse".

These tests MUST FAIL until production code at autofix/cli/policy_command.py
lands.
"""
# seg-6 validated under task-20260506-002 (clean-slate-cli-cutover).

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_policy(root: Path, data: Any) -> Path:
    """Write data as JSON to <root>/.autofix/autofix-policy.json."""
    autofix_dir = root / ".autofix"
    autofix_dir.mkdir(parents=True, exist_ok=True)
    policy_path = autofix_dir / "autofix-policy.json"
    policy_path.write_text(json.dumps(data), encoding="utf-8")
    return policy_path


def _write_raw_policy(root: Path, raw_text: str) -> Path:
    """Write raw text (possibly invalid JSON) to the policy path."""
    autofix_dir = root / ".autofix"
    autofix_dir.mkdir(parents=True, exist_ok=True)
    policy_path = autofix_dir / "autofix-policy.json"
    policy_path.write_text(raw_text, encoding="utf-8")
    return policy_path


# ---------------------------------------------------------------------------
# AC 9: no flag -> argparse exits 2
# ---------------------------------------------------------------------------


class TestPolicyNoFlag:
    """AC 9 — invoking policy with no flag exits 2 (argparse mutual-exclusive
    group's 'one of the arguments is required' behavior)."""

    def test_policy_no_flag_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 9: no --show and no --validate -> argparse exit 2."""
        from autofix.cli.policy_command import add_arguments

        parser = argparse.ArgumentParser()
        add_arguments(parser)

        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args([])

        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# AC 10: --show happy path and missing-file case
# ---------------------------------------------------------------------------


class TestPolicyShow:
    """AC 10 — --show emits sorted JSON to stdout and exits 0. When the file
    is missing, exits 1 with stderr substring 'autofix-policy.json not found'."""

    def test_show_emits_sorted_json_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 10: --show reads policy, dumps sorted JSON, exits 0."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {"llm_tiered": False, "min_priority_for_llm_triage": 5})

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--show"])

        exit_code = run(ns, root=tmp_path)

        assert exit_code == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["llm_tiered"] is False
        assert parsed["min_priority_for_llm_triage"] == 5

    def test_show_output_is_sorted_keys(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 10: keys in stdout are sorted (sort_keys=True)."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {"z_key": 1, "a_key": 2, "m_key": 3})

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--show"])
        run(ns, root=tmp_path)

        captured = capsys.readouterr()
        # Verify that the raw output has keys in alphabetical order
        lines = captured.out.strip().splitlines()
        key_order = [
            line.split(":")[0].strip().strip('"')
            for line in lines
            if ":" in line and line.strip().startswith('"')
        ]
        assert key_order == sorted(key_order), (
            f"expected sorted keys, got order: {key_order}"
        )

    def test_show_missing_file_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 10: missing autofix-policy.json -> exit 1."""
        from autofix.cli.policy_command import add_arguments, run

        # Deliberately do NOT create the policy file
        assert not (tmp_path / ".autofix" / "autofix-policy.json").exists()

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--show"])

        exit_code = run(ns, root=tmp_path)

        assert exit_code == 1

    def test_show_missing_file_stderr_substring(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 10: missing file produces stderr with 'autofix-policy.json not found'."""
        from autofix.cli.policy_command import add_arguments, run

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--show"])

        run(ns, root=tmp_path)

        captured = capsys.readouterr()
        assert "autofix-policy.json not found" in captured.err, (
            f"expected 'autofix-policy.json not found' in stderr, got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# AC 11: --validate
# ---------------------------------------------------------------------------


class TestPolicyValidate:
    """AC 11 — --validate structural checker."""

    def test_validate_valid_policy_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: all known keys with correct types -> exit 0."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(
            tmp_path,
            {
                "llm_tiered": True,
                "state_migration": {"legacy_findings_enabled": True},
                "min_priority_for_llm_triage": 3,
                "index": {"embedding_sidecar": {}},
            },
        )
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])

        exit_code = run(ns, root=tmp_path)
        assert exit_code == 0

    def test_validate_empty_policy_exits_zero(
        self, tmp_path: Path
    ) -> None:
        """AC 11: empty dict policy (no known keys present) -> exit 0."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {})
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])

        assert run(ns, root=tmp_path) == 0

    def test_validate_unknown_keys_ignored(self, tmp_path: Path) -> None:
        """AC 11: unknown top-level keys are silently accepted."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {"future_key": "whatever", "another": 42})
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])

        assert run(ns, root=tmp_path) == 0

    def test_validate_wrong_type_llm_tiered_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: llm_tiered is a string instead of bool -> exit 2."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {"llm_tiered": "yes"})
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])

        exit_code = run(ns, root=tmp_path)
        assert exit_code == 2

    def test_validate_wrong_type_llm_tiered_stderr_substring(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: wrong-type llm_tiered -> stderr contains 'policy.llm_tiered:'."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {"llm_tiered": "yes"})
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])
        run(ns, root=tmp_path)

        captured = capsys.readouterr()
        assert "policy.llm_tiered:" in captured.err, (
            f"expected 'policy.llm_tiered:' in stderr, got: {captured.err!r}"
        )

    def test_validate_wrong_type_state_migration_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: state_migration is a list instead of dict -> exit 2."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {"state_migration": ["a", "b"]})
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])

        exit_code = run(ns, root=tmp_path)
        assert exit_code == 2

    def test_validate_wrong_type_state_migration_stderr_substring(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: wrong-type state_migration -> stderr contains 'policy.state_migration:'."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {"state_migration": ["a", "b"]})
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])
        run(ns, root=tmp_path)

        captured = capsys.readouterr()
        assert "policy.state_migration:" in captured.err, (
            f"expected 'policy.state_migration:' in stderr, got: {captured.err!r}"
        )

    def test_validate_wrong_type_min_priority_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: min_priority_for_llm_triage is a string -> exit 2."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {"min_priority_for_llm_triage": "high"})
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])

        exit_code = run(ns, root=tmp_path)
        assert exit_code == 2

    def test_validate_wrong_type_min_priority_stderr_substring(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: wrong-type min_priority -> stderr 'policy.min_priority_for_llm_triage:'."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {"min_priority_for_llm_triage": "high"})
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])
        run(ns, root=tmp_path)

        captured = capsys.readouterr()
        assert "policy.min_priority_for_llm_triage:" in captured.err, (
            f"expected 'policy.min_priority_for_llm_triage:' in stderr, "
            f"got: {captured.err!r}"
        )

    def test_validate_min_priority_float_is_accepted(
        self, tmp_path: Path
    ) -> None:
        """AC 11: min_priority_for_llm_triage as float is valid."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {"min_priority_for_llm_triage": 3.5})
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])

        assert run(ns, root=tmp_path) == 0

    def test_validate_index_not_dict_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: index is a string instead of dict -> exit 2."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {"index": "bad"})
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])

        exit_code = run(ns, root=tmp_path)
        assert exit_code == 2

    def test_validate_index_not_dict_stderr_substring(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: wrong-type index -> stderr 'policy.index:'."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, {"index": "bad"})
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])
        run(ns, root=tmp_path)

        captured = capsys.readouterr()
        assert "policy.index:" in captured.err, (
            f"expected 'policy.index:' in stderr, got: {captured.err!r}"
        )

    def test_validate_non_dict_top_level_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: top-level JSON is array, not object -> exit 2 with
        'policy file does not parse as a JSON object'."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, [{"llm_tiered": False}])
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])

        exit_code = run(ns, root=tmp_path)
        assert exit_code == 2

    def test_validate_non_dict_top_level_stderr_substring(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: non-dict top level -> stderr 'policy file does not parse as a JSON object'."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(tmp_path, [{"llm_tiered": False}])
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])
        run(ns, root=tmp_path)

        captured = capsys.readouterr()
        assert "policy file does not parse as a JSON object" in captured.err, (
            f"expected 'policy file does not parse as a JSON object' in stderr, "
            f"got: {captured.err!r}"
        )

    def test_validate_unparseable_json_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: malformed JSON (truncated) -> exit 2 with stderr 'does not parse'."""
        from autofix.cli.policy_command import add_arguments, run

        _write_raw_policy(tmp_path, '{"llm_tiered": tru')  # truncated/invalid
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])

        exit_code = run(ns, root=tmp_path)
        assert exit_code == 2

    def test_validate_unparseable_json_stderr_substring(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: malformed JSON -> stderr contains 'does not parse'."""
        from autofix.cli.policy_command import add_arguments, run

        _write_raw_policy(tmp_path, "not json at all!!!")
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])
        run(ns, root=tmp_path)

        captured = capsys.readouterr()
        assert "does not parse" in captured.err, (
            f"expected 'does not parse' in stderr, got: {captured.err!r}"
        )

    def test_validate_multiple_type_errors_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 11: multiple wrong types -> each produces a separate stderr line."""
        from autofix.cli.policy_command import add_arguments, run

        _write_policy(
            tmp_path,
            {
                "llm_tiered": "yes",  # should be bool
                "state_migration": 42,  # should be dict
            },
        )
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])
        run(ns, root=tmp_path)

        captured = capsys.readouterr()
        assert "policy.llm_tiered:" in captured.err
        assert "policy.state_migration:" in captured.err

    def test_validate_missing_file_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC 10+11: missing policy file -> exit 1 for --validate too."""
        from autofix.cli.policy_command import add_arguments, run

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--validate"])

        exit_code = run(ns, root=tmp_path)
        assert exit_code == 1
