"""Tests for the per-scan adapter-registration emission state
(task-010 AC 6-9).

Covers:
  AC 6 — ``_ADAPTER_REGISTERED_EMITTED_CV`` contextvar default is None.
  AC 7 — ``reset_adapter_emission_state`` raises ``RuntimeError`` without
         ``AUTOFIX_TESTING=1`` and succeeds with it.
  AC 8 — two nested contexts track separate emission sets.
  AC 9 — thread-safe read-and-set: 20 threads racing on the same name
         observe exactly one True return; the others return False.
"""

from __future__ import annotations

import contextvars
import threading

import pytest


def _mod():
    return pytest.importorskip("autofix.languages.adapter_emission")


# ---------------------------------------------------------------------------
# AC 6 — default None contextvar.
# ---------------------------------------------------------------------------


def test_contextvar_default_is_none_in_fresh_context() -> None:
    """AC 6: in a freshly-copied context, the emission-set contextvar
    returns None — no set has been materialised yet."""
    ae = _mod()

    def _probe() -> object:
        return ae._ADAPTER_REGISTERED_EMITTED_CV.get()

    result = contextvars.Context().run(_probe)
    assert result is None, (
        f"contextvar default must be None in a fresh context; got {result!r}"
    )


# ---------------------------------------------------------------------------
# AC 8 — two nested contexts track separate sets.
# ---------------------------------------------------------------------------


def test_two_nested_contexts_track_separate_sets() -> None:
    """AC 8: marks made in ``Context().run(...)`` do NOT leak into a
    sibling context. Each ``Context()`` copy starts with the contextvar
    at its default (None) and materialises its own set."""
    ae = _mod()

    observations: dict[str, list[bool]] = {"ctx_a": [], "ctx_b": []}

    def _in_ctx_a() -> None:
        # First call is True, second call on the SAME name is False —
        # this validates the contextvar within this context works.
        observations["ctx_a"].append(ae.mark_adapter_registered("foo"))
        observations["ctx_a"].append(ae.mark_adapter_registered("foo"))
        observations["ctx_a"].append(ae.mark_adapter_registered("bar"))

    def _in_ctx_b() -> None:
        # A fresh context means "foo" is NOT yet marked here even though
        # _in_ctx_a marked it in the sibling context.
        observations["ctx_b"].append(ae.mark_adapter_registered("foo"))
        observations["ctx_b"].append(ae.mark_adapter_registered("baz"))

    # Run each in its own independently-copied Context so sets are not
    # shared.
    contextvars.Context().run(_in_ctx_a)
    contextvars.Context().run(_in_ctx_b)

    # Context A: True, False, True
    assert observations["ctx_a"] == [True, False, True], observations["ctx_a"]
    # Context B: "foo" is first-seen here → True; "baz" is also first-seen → True.
    assert observations["ctx_b"] == [True, True], observations["ctx_b"]


def test_mark_returns_false_on_second_call_same_context() -> None:
    """AC 8 (property): within a single context, the second call for the
    same name returns False, guarding the "emit exactly once per scan"
    contract."""
    ae = _mod()

    results: list[bool] = []

    def _inside() -> None:
        results.append(ae.mark_adapter_registered("jsts"))
        results.append(ae.mark_adapter_registered("jsts"))
        results.append(ae.mark_adapter_registered("jsts"))

    contextvars.Context().run(_inside)
    assert results == [True, False, False], results


# ---------------------------------------------------------------------------
# AC 9 — thread-safe race: 20 threads, exactly one True.
# ---------------------------------------------------------------------------


def test_mark_adapter_registered_is_thread_safe_under_race() -> None:
    """AC 9: spawn 20 threads that each call ``mark_adapter_registered("foo")``
    against a shared emission set; exactly one thread observes ``True``
    and the other 19 observe ``False``.

    To actually exercise the lock, all threads must share the *same*
    underlying ``set`` object in the contextvar — otherwise each
    thread's fresh-context default (None) would cause every thread to
    materialise its own set and win the race independently. We pre-bind
    the contextvar to a shared set inside each worker so the 20 threads
    race on the module lock's critical section as in production.
    """
    ae = _mod()

    N = 20
    outcomes: list[bool] = []
    outcomes_lock = threading.Lock()
    barrier = threading.Barrier(N)

    # A single set shared across all worker threads. Each worker
    # explicitly binds the contextvar to this same object before calling
    # ``mark_adapter_registered`` so the 20 threads contend on one
    # critical section in the module lock.
    shared_set: set[str] = set()

    def _worker() -> None:
        # Bind the contextvar in THIS thread to the shared set so the
        # module's ``current = _ADAPTER_REGISTERED_EMITTED_CV.get()``
        # observes the same object every worker is mutating.
        ae._ADAPTER_REGISTERED_EMITTED_CV.set(shared_set)
        # Synchronise all threads up to the lock-contention point so
        # the test actually stresses the lock.
        barrier.wait(timeout=10)
        got = ae.mark_adapter_registered("foo")
        with outcomes_lock:
            outcomes.append(got)

    threads = [threading.Thread(target=_worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), f"thread {t.name} failed to complete"

    assert len(outcomes) == N, f"expected {N} outcomes, got {len(outcomes)}"
    true_count = sum(1 for v in outcomes if v is True)
    false_count = sum(1 for v in outcomes if v is False)
    assert true_count == 1, (
        f"exactly one thread must win the race; got True={true_count}, "
        f"False={false_count}, outcomes={outcomes!r}"
    )
    assert false_count == N - 1
    # And the shared set now contains exactly the one name.
    assert shared_set == {"foo"}


# ---------------------------------------------------------------------------
# AC 7 — reset guard on AUTOFIX_TESTING.
# ---------------------------------------------------------------------------


def test_reset_requires_testing_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 7: ``reset_adapter_emission_state`` raises RuntimeError when
    ``AUTOFIX_TESTING`` is unset (or not exactly ``"1"``)."""
    ae = _mod()

    monkeypatch.delenv("AUTOFIX_TESTING", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        ae.reset_adapter_emission_state()
    assert "AUTOFIX_TESTING" in str(excinfo.value)

    # Wrong value also fails — not just any non-empty string.
    monkeypatch.setenv("AUTOFIX_TESTING", "0")
    with pytest.raises(RuntimeError):
        ae.reset_adapter_emission_state()

    monkeypatch.setenv("AUTOFIX_TESTING", "true")
    with pytest.raises(RuntimeError):
        ae.reset_adapter_emission_state()


def test_reset_succeeds_under_testing_env_and_clears_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 7: with ``AUTOFIX_TESTING=1``, ``reset_adapter_emission_state``
    succeeds AND clears the contextvar binding to None so the next
    ``mark_adapter_registered`` call returns True again."""
    ae = _mod()
    monkeypatch.setenv("AUTOFIX_TESTING", "1")

    outcomes: list[object] = []

    def _inside() -> None:
        # First mark: True.
        outcomes.append(ae.mark_adapter_registered("x"))
        # Second mark of same name: False (emission already recorded).
        outcomes.append(ae.mark_adapter_registered("x"))
        # Reset under the testing flag: no exception.
        ae.reset_adapter_emission_state()
        # The contextvar is now explicitly None (the helper stores None,
        # not a fresh empty set).
        outcomes.append(ae._ADAPTER_REGISTERED_EMITTED_CV.get())
        # After reset, a subsequent mark succeeds again.
        outcomes.append(ae.mark_adapter_registered("x"))

    contextvars.Context().run(_inside)

    assert outcomes[0] is True
    assert outcomes[1] is False
    assert outcomes[2] is None, (
        f"reset must set the contextvar back to None; got {outcomes[2]!r}"
    )
    assert outcomes[3] is True


def test_reset_does_not_raise_when_state_already_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotency guard: calling reset on a fresh context that never
    marked anything must not raise. This defends against a regression
    where reset assumes the set was materialised."""
    ae = _mod()
    monkeypatch.setenv("AUTOFIX_TESTING", "1")

    def _inside() -> None:
        # Never called mark_adapter_registered — contextvar is at default.
        assert ae._ADAPTER_REGISTERED_EMITTED_CV.get() is None
        ae.reset_adapter_emission_state()  # must not raise
        assert ae._ADAPTER_REGISTERED_EMITTED_CV.get() is None

    contextvars.Context().run(_inside)
