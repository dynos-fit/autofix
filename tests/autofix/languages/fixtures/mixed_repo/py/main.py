"""Fixture module for task-20260417-006 pipeline-integration tests.

Contains exactly one top-level import (``unused_module``) that is never
referenced anywhere in the file. The ``unused-import.intra-file``
analyzer must therefore emit exactly one ``CandidateFinding`` for this
module. Used by ``test_pipeline_integration.py`` to pin byte-identical
analyzer output across the pre- and post-task-006 pipeline dispatcher.
"""
import unused_module  # noqa: F401 — deliberately unreferenced


def greet(name: str) -> str:
    return f"hello, {name}"


if __name__ == "__main__":
    print(greet("world"))
