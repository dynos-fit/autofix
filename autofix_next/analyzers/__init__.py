"""Analyzers subpackage.

Splits by cost class: ``analyzers.cheap`` hosts O(file)
AST/symbol-table rules that never exit the process. Expensive/global
analyzers live in sibling subpackages introduced by later segments.
"""
