import json
import sys
from pathlib import Path

from autofix.agent_loop import _execute_action, execute_action
from autofix.benchmarking import benchmark_trace_llm, benchmark_trace_tool
from benchmarks.agent_bench.autofix_adapter import AutofixBenchmarkConfig, build_agent, install
from benchmarks.agent_bench.tasks_to_fixtures import materialize_agent_bench_fixtures
from benchmarks.agent_bench.run_autofix_benchmark import _ensure_agent_bench_importable


def test_optional_benchmark_decorators_are_noops_without_agent_bench() -> None:
    @benchmark_trace_llm
    def call_model(prompt: str) -> str:
        return prompt.upper()

    @benchmark_trace_tool
    def call_tool(name: str) -> str:
        return name + ":ok"

    assert call_model("hi") == "HI"
    assert call_tool("search") == "search:ok"


def test_execute_action_public_alias_preserves_private_name() -> None:
    assert execute_action is _execute_action


def test_build_agent_returns_callable() -> None:
    agent = build_agent(AutofixBenchmarkConfig())
    assert callable(agent)


def test_build_agent_supports_agent_bench_cli_contract() -> None:
    agent = build_agent(model="default", max_steps=4, timeout=30)
    assert callable(agent)


def test_build_agent_reads_backend_defaults_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AUTOFIX_BENCH_BACKEND", "openai_compatible")
    monkeypatch.setenv("AUTOFIX_BENCH_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("AUTOFIX_BENCH_API_KEY", "ollama")
    monkeypatch.setenv("AUTOFIX_BENCH_MAX_FIX_ATTEMPTS", "5")

    agent = build_agent(model="default", max_steps=4, timeout=30)
    assert callable(agent)


def test_benchmark_install_is_a_noop() -> None:
    assert install() is None


def test_materialize_agent_bench_fixtures_maps_task_format(tmp_path: Path) -> None:
    task_dir = tmp_path / "tasks" / "bugfix_take_limit"
    repo_dir = task_dir / "repo"
    (repo_dir / "src").mkdir(parents=True)
    (repo_dir / "tests").mkdir(parents=True)
    (repo_dir / "src" / "calc.py").write_text("def take_limit(items, limit):\n    return items[:limit]\n", encoding="utf-8")
    (repo_dir / "tests" / "test_calc.py").write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "id": "bugfix_take_limit",
                "title": "Fix take_limit",
                "category": "bugfix",
                "difficulty": "easy",
                "instruction": "Fix `src/calc.py`.",
                "scope": {
                    "allowed_files": ["src/calc.py"],
                    "forbidden_files": ["tests"],
                },
                "verification": [
                    {
                        "name": "pytest",
                        "command": "{python_executable} -m pytest -q",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    fixtures_root = tmp_path / "fixtures"
    fixture_ids = materialize_agent_bench_fixtures(task_dir.parent, fixtures_root)

    assert fixture_ids == ["bugfix_take_limit"]
    fixture_json = json.loads((fixtures_root / "bugfix_take_limit" / "fixture.json").read_text(encoding="utf-8"))
    assert fixture_json["id"] == "bugfix_take_limit"
    assert fixture_json["name"] == "Fix take_limit"
    assert fixture_json["description"] == "Fix `src/calc.py`."
    assert fixture_json["scope"]["allowed_files"] == ["src/calc.py"]
    assert fixture_json["scope"]["forbidden_files"] == ["tests"]
    assert fixture_json["test_command"] == [sys.executable, "-m", "pytest", "-q"]
    copied_source = fixtures_root / "bugfix_take_limit" / "bugged" / "src" / "calc.py"
    assert copied_source.exists()


def test_agent_bench_import_helper_works_with_installed_or_sibling(tmp_path: Path) -> None:
    """The helper should succeed whether agent_bench is pip-installed or found via path."""
    # If agent_bench is already installed as a package, the helper returns
    # immediately without touching sys.path — that's the happy path.
    # If not installed, it falls back to sibling checkout discovery.
    # We test that _ensure_agent_bench_importable doesn't raise in either case.
    package_root = tmp_path / "agent-bench"
    module_dir = package_root / "agent_bench"
    module_dir.mkdir(parents=True)
    (module_dir / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")

    # Should not raise regardless of whether agent_bench is installed.
    _ensure_agent_bench_importable(str(package_root))
