"""LLM scheduler gate (AC #7, #8, #14, #15, #24).

This module is the SOLE boundary between ``autofix_next`` and the locked
LLM seam exposed by :mod:`autofix.llm_backend`. Every other module under
``autofix_next/`` reaches the LLM through this scheduler. A grep test in
``tests/autofix_next/test_llm_scheduler.py::test_run_prompt_is_only_llm_seam``
enforces the boundary.

Gates applied, in order:

1. **Suppression glob** (AC #14) — the ``suppressions`` key of
   ``.autofix/autofix-policy.json`` lists ``fnmatch``-style patterns.
   Packets whose ``primary_symbol`` file path matches any pattern are
   dropped with an ``LLMCallGated{decision="skipped_suppressed"}`` event
   and no LLM call is made.
2. **Duplicate prefix hash** (AC #15) — an in-memory ``set`` of
   ``prompt_prefix_hash`` values skips any packet whose hash has already
   been sent this process. Emits
   ``LLMCallGated{decision="skipped_duplicate_hash"}``.
3. **Promotion** — the prompt is assembled as the raw Markdown template
   (cache-friendly prefix) FIRST, followed by a newline, followed by the
   JSON-serialized 7-key packet (AC #24). The scheduler calls
   :func:`autofix.llm_backend.run_prompt` exactly once and emits either
   ``decision="promoted"`` (success) or ``decision="promoted_failed"``
   (exception or non-zero returncode).

The hash is recorded in the dedup set BEFORE the LLM call so that a
failing call does not trigger infinite retries if a caller re-submits
the same packet.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

# NOTE: this is the single authorized import from ``autofix.*`` anywhere
# under ``autofix_next/``. Enforced by
# ``tests/autofix_next/test_llm_scheduler.py::test_run_prompt_is_only_llm_seam``.
import autofix.llm_backend as _llm_backend
from autofix.llm_backend import LLMBackendConfig, LLMResult

from autofix_next.evidence.fingerprints import canonical_json_bytes
from autofix_next.evidence.schema import EvidencePacket
from autofix_next.telemetry import events_log

# Decision literal shared by the scheduler's public surface and by the
# ``LLMCallGated`` event payload.
ScheduleDecisionName = Literal[
    "promoted",
    "skipped_suppressed",
    "skipped_duplicate_hash",
    "promoted_failed",
]


@dataclass(slots=True)
class ScheduleDecision:
    """The outcome of a single :meth:`Scheduler.schedule` call.

    Attributes
    ----------
    decision:
        One of ``"promoted"``, ``"skipped_suppressed"``,
        ``"skipped_duplicate_hash"``, ``"promoted_failed"``.
    result:
        The :class:`LLMResult` returned by ``run_prompt`` on success; ``None``
        for any skip or failure path.
    reason:
        Optional short prose describing why the decision was made. Set for
        skips and failures; ``None`` for successful promotions.
    """

    decision: ScheduleDecisionName
    result: LLMResult | None
    reason: str | None = None


class Scheduler:
    """Gate LLM calls behind suppression + dedup checks and emit telemetry.

    One instance per scan. The dedup set is per-instance (an in-memory
    ``set[str]``), so two separate scans can legitimately fire the same
    ``prompt_prefix_hash`` again if the caller constructs two schedulers.
    """

    def __init__(
        self,
        root: Path,
        policy_path: Path | None = None,
        llm_config: LLMBackendConfig | None = None,
        timeout: int = 60,
        prompt_template_path: Path | None = None,
    ) -> None:
        self._root: Path = Path(root)
        self._timeout: int = int(timeout)
        self._config: LLMBackendConfig = llm_config or LLMBackendConfig()
        self._called_hashes: set[str] = set()

        resolved_policy = (
            Path(policy_path)
            if policy_path is not None
            else self._root / ".autofix" / "autofix-policy.json"
        )
        self._suppressions: list[str] = self._load_suppressions(resolved_policy)

        resolved_template = (
            Path(prompt_template_path)
            if prompt_template_path is not None
            else Path(__file__).parent / "prompts" / "unused_import_review.md"
        )
        try:
            self._prompt_template: str = resolved_template.read_text(
                encoding="utf-8"
            )
        except OSError as exc:
            # A missing prompt template is a deployment bug, not a data
            # problem. Surface a clear error rather than falling back to
            # an empty prompt that would silently confuse the LLM.
            raise FileNotFoundError(
                f"LLM prompt template missing: {resolved_template}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Policy loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_suppressions(policy_path: Path) -> list[str]:
        """Return the ``suppressions`` list from the policy file, or []."""
        if not policy_path.is_file():
            return []
        try:
            raw = policy_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"cannot read autofix-policy.json at {policy_path}: {exc}"
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"autofix-policy.json at {policy_path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            return []
        suppressions = data.get("suppressions")
        if not isinstance(suppressions, list):
            return []
        # Defensive: reject non-string patterns silently rather than
        # letting fnmatch crash on a malformed policy file.
        return [p for p in suppressions if isinstance(p, str)]

    # ------------------------------------------------------------------
    # Suppression check
    # ------------------------------------------------------------------

    def _matches_suppression(self, primary_symbol: str) -> bool:
        """Return True if the path portion of ``primary_symbol`` matches a glob.

        ``primary_symbol`` is ``"<path>::<bound_name>"``. Only the path
        portion is matched; the bound name is irrelevant to path-based
        suppression rules.
        """
        if "::" in primary_symbol:
            path_part = primary_symbol.split("::", 1)[0]
        else:
            path_part = primary_symbol
        for pattern in self._suppressions:
            if fnmatch.fnmatch(path_part, pattern):
                return True
        return False

    # ------------------------------------------------------------------
    # Telemetry helpers
    # ------------------------------------------------------------------

    def _emit_gated_event(
        self,
        *,
        decision: ScheduleDecisionName,
        packet: EvidencePacket,
        reason: str | None = None,
    ) -> None:
        """Append an ``LLMCallGated`` envelope row with the decision payload.

        The attribute lookup on ``events_log.append_event`` is intentional
        (rather than a ``from events_log import append_event``) so tests
        can monkeypatch the symbol on the source module.
        """
        payload: dict[str, Any] = {
            "event_type": "LLMCallGated",
            "repo_id": self._root.name,
            "decision": decision,
            "prompt_prefix_hash": packet.prompt_prefix_hash,
            "primary_symbol": packet.primary_symbol,
            "rule_id": packet.rule_id,
        }
        if reason is not None:
            payload["reason"] = reason
        try:
            events_log.append_event(self._root, "LLMCallGated", payload)
        except OSError:
            # Telemetry-write failures must not break the scan. The
            # scheduler still returns a meaningful decision to the
            # caller; the operator will notice the missing rows in the
            # events.jsonl tail.
            pass

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def schedule(self, packet: EvidencePacket) -> ScheduleDecision:
        """Apply gates and (if none trip) call the LLM exactly once.

        Parameters
        ----------
        packet:
            The :class:`EvidencePacket` built by
            :func:`autofix_next.evidence.builder.build_packet`.

        Returns
        -------
        ScheduleDecision
            The outcome of this single scheduling attempt.
        """
        # Gate 1: suppression glob.
        if self._matches_suppression(packet.primary_symbol):
            reason = "path matches a configured suppression glob"
            self._emit_gated_event(
                decision="skipped_suppressed", packet=packet, reason=reason
            )
            return ScheduleDecision(
                decision="skipped_suppressed", result=None, reason=reason
            )

        # Gate 2: duplicate prefix hash.
        if packet.prompt_prefix_hash in self._called_hashes:
            reason = "prompt_prefix_hash already seen this scan"
            self._emit_gated_event(
                decision="skipped_duplicate_hash", packet=packet, reason=reason
            )
            return ScheduleDecision(
                decision="skipped_duplicate_hash", result=None, reason=reason
            )

        # Assemble the prompt: template FIRST (cache-friendly prefix),
        # JSON packet LAST (variable tail). AC #24.
        #
        # We route the serialization through ``canonical_json_bytes``
        # (the repo's single permitted JSON-to-bytes path for hashable
        # inputs; see AC #17). Decoding to ``str`` for concatenation is
        # safe because ``canonical_json_bytes`` emits UTF-8 by contract.
        packet_json_text = canonical_json_bytes(packet.to_dict()).decode("utf-8")
        prompt = self._prompt_template + "\n" + packet_json_text

        # Record the hash BEFORE the call so a failing call does not
        # invite an infinite retry loop from a confused caller.
        self._called_hashes.add(packet.prompt_prefix_hash)

        # Attribute-lookup call style so ``monkeypatch.setattr(
        # "autofix.llm_backend.run_prompt", fake)`` in tests takes effect.
        try:
            result = _llm_backend.run_prompt(
                prompt,
                model=None,
                config=self._config,
                timeout=self._timeout,
                cwd=self._root,
            )
        except Exception as exc:  # noqa: BLE001 - telemetry reports the class.
            reason = f"{type(exc).__name__}: {exc}"
            self._emit_gated_event(
                decision="promoted_failed", packet=packet, reason=reason
            )
            return ScheduleDecision(
                decision="promoted_failed", result=None, reason=reason
            )

        if not isinstance(result, LLMResult) or result.returncode != 0:
            rc = getattr(result, "returncode", "unknown")
            reason = f"returncode={rc}"
            self._emit_gated_event(
                decision="promoted_failed", packet=packet, reason=reason
            )
            return ScheduleDecision(
                decision="promoted_failed", result=None, reason=reason
            )

        self._emit_gated_event(decision="promoted", packet=packet)
        return ScheduleDecision(decision="promoted", result=result)


__all__ = [
    "Scheduler",
    "ScheduleDecision",
    "ScheduleDecisionName",
]
