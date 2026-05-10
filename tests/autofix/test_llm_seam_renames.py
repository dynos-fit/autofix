"""PROACTIVE-10: LLMSeamUnavailableError rename + back-compat alias + Protocol.

The error class was renamed from ``AnalyzerSeamUnavailableError`` to
``LLMSeamUnavailableError`` (use-case-neutral — the same error fires
from non-analyzer callers like ``autofix.repair.llm_patcher``).

The old name remains as an alias so external callers and tests that
historically imported ``AnalyzerSeamUnavailableError`` continue to
work transparently.

This file pins the rename invariants:

1. Both names import to the *same* class object.
2. ``raise OldName`` can be caught by ``except NewName`` and
   vice versa (proven by `1` automatically — they're literally the
   same class).
3. The new ``autofix.repair.contracts.LLMPatchingClient`` Protocol
   is structurally satisfied by ``autofix.llm.scheduler.Scheduler``.
"""
from __future__ import annotations

from autofix.llm.scheduler import (
    AnalyzerSeamUnavailableError,
    LLMSeamUnavailableError,
    Scheduler,
)
from autofix.repair.contracts import LLMPatchingClient


def test_aliases_resolve_to_same_class() -> None:
    """``AnalyzerSeamUnavailableError`` and ``LLMSeamUnavailableError``
    are the SAME class object — a back-compat alias, not a subclass.
    """
    assert AnalyzerSeamUnavailableError is LLMSeamUnavailableError


def test_raise_old_caught_by_new() -> None:
    """``raise AnalyzerSeamUnavailableError`` is caught by
    ``except LLMSeamUnavailableError``.
    """
    try:
        raise AnalyzerSeamUnavailableError("legacy import path")
    except LLMSeamUnavailableError as exc:
        assert "legacy import path" in str(exc)


def test_raise_new_caught_by_old() -> None:
    """And the symmetric direction — old except still catches the new raise."""
    try:
        raise LLMSeamUnavailableError("new import path")
    except AnalyzerSeamUnavailableError as exc:
        assert "new import path" in str(exc)


def test_scheduler_satisfies_llm_patching_client_protocol() -> None:
    """``Scheduler`` structurally satisfies the ``LLMPatchingClient``
    Protocol (defined in ``autofix.repair.contracts``).

    The Protocol is ``@runtime_checkable``, so ``isinstance(s,
    LLMPatchingClient)`` works without inheritance — it just verifies
    the named methods exist.
    """
    # Don't fully construct (Scheduler reads disk) — instead verify
    # the method exists on the class itself.
    assert hasattr(Scheduler, "invoke_judgment")
    # The method's runtime presence is what the Protocol cares about.
    # Attribute lookup via `runtime_checkable` resolves at instance time;
    # an instance check on a real Scheduler would also pass.


def test_protocol_rejects_object_without_invoke_judgment() -> None:
    """A bare object does NOT satisfy the Protocol."""
    assert not isinstance(object(), LLMPatchingClient)


def test_protocol_accepts_minimal_mock() -> None:
    """A minimal duck-typed object with ``invoke_judgment`` satisfies it."""

    class _Mock:
        def invoke_judgment(self, prompt: str, *, model: str) -> str:
            return ""

    assert isinstance(_Mock(), LLMPatchingClient)
