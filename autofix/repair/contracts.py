"""Public adapter contracts for the repair subsystem.

The repair pipeline (``autofix.repair.llm_patcher``,
``autofix.repair.coordinator``) currently constructs an
``autofix.llm.scheduler.Scheduler`` directly when it needs to call
out to the LLM. That works today, but it couples the subsystem to
a concrete class and would block any future "lift repair into
another project" treatment.

This module defines a typed Protocol that names the EXACT method
shape the repair code uses. The concrete ``Scheduler`` already
satisfies the Protocol structurally (no inheritance is needed) —
this module is the seam that lets a future caller inject a
test double / alternative LLM client without changing any
production code.

Why this lives here, not in ``autofix.llm``: the Protocol is shaped
by the *repair use case* (just one method: ``invoke_judgment``),
not by the LLM module's full surface. Putting it here matches
the same pattern the crawler subsystem uses
(``autofix.crawl.contracts.GitLogAdapter`` /
``autofix.crawl.contracts.CallGraphAdapter``) — the consumer owns
the Protocol that names what it needs.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMPatchingClient(Protocol):
    """Read-only contract for the LLM seam used by ``llm_patcher``.

    The single method ``invoke_judgment(prompt, *, model)`` takes a
    pre-assembled prompt string and a model identifier; it returns
    the LLM's raw text response. On unavailability (binary missing,
    API key unconfigured, etc.), it raises
    :class:`autofix.llm.scheduler.LLMSeamUnavailableError`.

    The concrete implementation today is
    :class:`autofix.llm.scheduler.Scheduler`. External integrators
    (or test doubles) can substitute any object exposing the same
    method shape — Python's Protocol mechanism does the rest.
    """

    def invoke_judgment(self, prompt: str, *, model: str) -> str: ...


__all__ = ["LLMPatchingClient"]
