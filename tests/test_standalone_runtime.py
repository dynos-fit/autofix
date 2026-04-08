from pathlib import Path

from autofix.dynos_backend import create_dynos_backend
from autofix.platform import aggregate_state_dir, persistent_project_dir, runtime_state_dir
from autofix.state import autofix_benchmarks_path, autofix_metrics_path, findings_path, scan_coverage_path
from autofix.runtime import dynos


def test_runtime_dirs_are_local(tmp_path: Path) -> None:
    assert runtime_state_dir(tmp_path) == tmp_path / ".autofix"
    assert persistent_project_dir(tmp_path) == tmp_path / ".autofix"
    assert aggregate_state_dir(tmp_path) == tmp_path / ".autofix" / "state"
    assert findings_path(tmp_path) == tmp_path / ".autofix" / "state" / "findings.json"
    assert scan_coverage_path(tmp_path) == tmp_path / ".autofix" / "state" / "scan-coverage.json"
    assert autofix_metrics_path(tmp_path) == tmp_path / ".autofix" / "state" / "metrics.json"
    assert autofix_benchmarks_path(tmp_path) == tmp_path / ".autofix" / "state" / "benchmarks.json"


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


def test_backend_dry_run_issue(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOFIX_DRY_RUN", "1")
    backend = create_dynos_backend(
        load_policy=lambda root: {"categories": {"llm-review": {"stats": {}}}},
        log=lambda msg: None,
        subprocess_module=__import__("subprocess"),
        shutil_module=__import__("shutil"),
        build_import_graph_fn=lambda root: {"edges": [], "pagerank": {}},
        get_neighbor_file_contents_fn=lambda *args, **kwargs: [],
        find_matching_template_fn=lambda root, finding: None,
    )
    finding = {"finding_id": "f-1", "description": "bug", "category": "llm-review", "severity": "medium", "evidence": {}}
    result = backend.open_github_issue(finding, tmp_path, {"categories": {"llm-review": {"stats": {}}}})
    assert result["dry_run"] is True
    assert result["issue_url"] == "dry-run://issue"


def test_backend_dry_run_fix(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOFIX_DRY_RUN", "1")
    backend = create_dynos_backend(
        load_policy=lambda root: {"categories": {"dead-code": {"stats": {}}}},
        log=lambda msg: None,
        subprocess_module=__import__("subprocess"),
        shutil_module=__import__("shutil"),
        build_import_graph_fn=lambda root: {"edges": [], "pagerank": {}},
        get_neighbor_file_contents_fn=lambda *args, **kwargs: [],
        find_matching_template_fn=lambda root, finding: None,
    )
    finding = {
        "finding_id": "f-2",
        "description": "dead import",
        "category": "dead-code",
        "severity": "low",
        "evidence": {"file": "sample.py"},
    }
    result = backend.autofix_finding(finding, tmp_path, {"categories": {"dead-code": {"stats": {}}}})
    assert result["dry_run"] is True
    assert result["pr_url"] == "dry-run://pr"
