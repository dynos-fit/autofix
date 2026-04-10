from autofix.backend import create_dynos_backend


def test_backend_uses_configured_fix_model() -> None:
    backend = create_dynos_backend(
        load_policy=lambda root: {"categories": {"dead-code": {"stats": {}}}},
        log=lambda msg: None,
        subprocess_module=__import__("subprocess"),
        shutil_module=__import__("shutil"),
        build_import_graph_fn=lambda root: {"edges": [], "pagerank": {}},
        get_neighbor_file_contents_fn=lambda *args, **kwargs: [],
        find_matching_template_fn=lambda root, finding: None,
        fix_model="qwen2.5-coder:7b",
    )
    assert backend.fix_model == "qwen2.5-coder:7b"


def test_backend_uses_prompt_budget_settings() -> None:
    backend = create_dynos_backend(
        load_policy=lambda root: {"categories": {"dead-code": {"stats": {}}}},
        log=lambda msg: None,
        subprocess_module=__import__("subprocess"),
        shutil_module=__import__("shutil"),
        build_import_graph_fn=lambda root: {"edges": [], "pagerank": {}},
        get_neighbor_file_contents_fn=lambda *args, **kwargs: [],
        find_matching_template_fn=lambda root, finding: None,
        fix_surrounding_lines=6,
        fix_neighbor_files=1,
        fix_neighbor_lines=24,
    )
    assert backend.fix_surrounding_lines == 6
    assert backend.fix_neighbor_files == 1
    assert backend.fix_neighbor_lines == 24
