"""TDD-first: assert relocated seams resolve under the new package.

Covers:
- AC 2: autofix/ contains the relocated seams (agent_loop, llm_backend, llm_io).
- AC 15: benchmark adapter imports succeed against the renamed package.
- AC 16: autofix/llm/scheduler.py imports the LLM seam via same-package import.

These tests MUST FAIL until task-20260506-003 lands.
"""
from __future__ import annotations


def test_autofix_llm_backend_resolves() -> None:
    """AC 2 + AC 16: autofix.llm_backend is importable and exposes run_prompt."""
    from autofix import llm_backend  # noqa: F401

    assert hasattr(llm_backend, "run_prompt"), (
        "autofix.llm_backend missing run_prompt — seam relocation incomplete."
    )


def test_autofix_agent_loop_resolves() -> None:
    """AC 2 + AC 15: autofix.agent_loop is importable; run_agent_loop callable present."""
    from autofix import agent_loop

    assert callable(getattr(agent_loop, "run_agent_loop", None))
    assert callable(getattr(agent_loop, "run_review_agent_loop", None))


def test_autofix_llm_io_prompts_dir_resolves() -> None:
    """AC 2: autofix/llm_io/prompts/ exists at the new location."""
    from pathlib import Path

    import autofix

    pkg_root = Path(autofix.__file__).resolve().parent
    prompts_dir = pkg_root / "llm_io" / "prompts"
    assert prompts_dir.is_dir(), (
        f"autofix/llm_io/prompts/ not found at {prompts_dir}"
    )


def test_benchmark_adapter_imports_resolve() -> None:
    """AC 15: benchmarks/agent_bench/autofix_adapter.py imports against renamed package."""
    import importlib

    spec = importlib.util.find_spec("benchmarks.agent_bench.autofix_adapter")
    if spec is None:
        # Different layout depending on test runner; locate manually.
        from pathlib import Path

        adapter_path = (
            Path(__file__).resolve().parents[2]
            / "benchmarks"
            / "agent_bench"
            / "autofix_adapter.py"
        )
        assert adapter_path.is_file()
        text = adapter_path.read_text(encoding="utf-8")
    else:
        from pathlib import Path as _P

        adapter_path = _P(spec.origin) if spec.origin else None
        assert adapter_path is not None
        text = adapter_path.read_text(encoding="utf-8")

    # The adapter must import from the new namespace, not the legacy autofix.
    assert ("autofix"+"_next") not in text, (
        "benchmark adapter still references the old namespace"
    )
    assert "autofix.agent_loop" in text or "from autofix import agent_loop" in text, (
        "benchmark adapter does not import autofix.agent_loop"
    )
    assert "autofix.llm_backend" in text or "from autofix import llm_backend" in text, (
        "benchmark adapter does not import autofix.llm_backend"
    )


def test_scheduler_imports_llm_seam_same_package() -> None:
    """AC 16: scheduler.py imports llm_backend without crossing into autofix."""
    from pathlib import Path

    import autofix

    sched = Path(autofix.__file__).resolve().parent / "llm" / "scheduler.py"
    text = sched.read_text(encoding="utf-8")
    assert ("autofix"+"_next") not in text, (
        "scheduler.py still references the old namespace"
    )
    assert "autofix.llm_backend" in text or "from autofix import llm_backend" in text or "from autofix.llm_backend" in text, (
        "scheduler.py does not import the LLM seam from autofix"
    )
