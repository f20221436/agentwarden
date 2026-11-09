from __future__ import annotations

import random

import pytest

from agentwarden.config import FaultConfig, RetryPolicy
from agentwarden.reliability.errors import PermanentToolError, ToolTimeout, TransientToolError, is_transient
from agentwarden.reliability.faults import FaultInjector
from agentwarden.reliability.retry import RetryOutcome, backoff_delay, call_with_retry

POLICY = RetryPolicy(max_attempts=4, base_delay_s=0.1, max_delay_s=1.0)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.sleeps.append(d)
        self.t += d


def flaky(fail_times: int, exc=TransientToolError):
    state = {"n": 0}

    def fn(attempt):
        state["n"] += 1
        if state["n"] <= fail_times:
            raise exc("boom")
        return "ok"
    return fn, state


def test_transient_errors_are_retried_then_succeed():
    clock = FakeClock()
    fn, state = flaky(2)
    out = RetryOutcome()
    assert call_with_retry(fn, policy=POLICY, retryable=True, deadline=100, outcome=out, rng=random.Random(1),
                           sleep=clock.sleep, clock=clock) == "ok"
    assert state["n"] == 3 and out.attempts == 3 and len(clock.sleeps) == 2


def test_permanent_errors_are_not_retried():
    clock = FakeClock()
    fn, state = flaky(5, PermanentToolError)
    with pytest.raises(PermanentToolError):
        call_with_retry(fn, policy=POLICY, retryable=True, deadline=100, outcome=RetryOutcome(),
                        sleep=clock.sleep, clock=clock)
    assert state["n"] == 1 and clock.sleeps == []


def test_non_idempotent_operations_are_never_retried():
    clock = FakeClock()
    fn, state = flaky(1)
    with pytest.raises(TransientToolError):
        call_with_retry(fn, policy=POLICY, retryable=False, deadline=100, outcome=RetryOutcome(),
                        sleep=clock.sleep, clock=clock)
    assert state["n"] == 1


def test_retries_disabled_means_single_attempt():
    fn, state = flaky(1)
    with pytest.raises(TransientToolError):
        call_with_retry(fn, policy=RetryPolicy(enabled=False), retryable=True, deadline=100,
                        outcome=RetryOutcome(), sleep=lambda d: None, clock=lambda: 0)
    assert state["n"] == 1


def test_max_attempts_exhausted():
    clock = FakeClock()
    fn, state = flaky(10)
    with pytest.raises(TransientToolError):
        call_with_retry(fn, policy=POLICY, retryable=True, deadline=100, outcome=RetryOutcome(),
                        sleep=clock.sleep, clock=clock)
    assert state["n"] == POLICY.max_attempts


def test_never_sleeps_past_deadline():
    clock = FakeClock()
    fn, state = flaky(10)
    with pytest.raises(TransientToolError):
        call_with_retry(fn, policy=RetryPolicy(max_attempts=50, base_delay_s=1, max_delay_s=1), retryable=True,
                        deadline=0.0001, outcome=RetryOutcome(), rng=random.Random(3), sleep=clock.sleep, clock=clock)
    assert state["n"] == 1 or clock.t < 0.0001 + 1


def test_backoff_is_bounded_full_jitter():
    rng = random.Random(0)
    for attempt in range(1, 10):
        for _ in range(50):
            d = backoff_delay(attempt, POLICY, rng)
            assert 0 <= d <= min(POLICY.max_delay_s, POLICY.base_delay_s * 2 ** (attempt - 1))


def test_error_taxonomy():
    assert is_transient(TransientToolError("x")) and is_transient(ToolTimeout("x")) and is_transient(TimeoutError())
    assert not is_transient(PermanentToolError("x")) and not is_transient(ValueError())


def test_fault_injection_is_deterministic_and_close_to_rate():
    cfg = FaultConfig(failure_rate=0.2)

    def pattern(seed):
        inj = FaultInjector(cfg, seed=seed, sleep=lambda s: None)
        out = []
        for i in range(2000):
            try:
                inj.maybe_fail(scope="task", tool="sql_query", ordinal=i, attempt=1)
                out.append(0)
            except TransientToolError:
                out.append(1)
        return out

    a, b = pattern(7), pattern(7)
    assert a == b                       # same seed -> same faults (fair ON vs OFF comparison)
    assert pattern(8) != a              # different seed -> different pattern
    assert 0.17 < sum(a) / len(a) < 0.23


def test_fault_injection_respects_tool_filter():
    inj = FaultInjector(FaultConfig(failure_rate=1.0, tools=["calculator"]), seed=1)
    inj.maybe_fail(scope="s", tool="sql_query", ordinal=1, attempt=1)  # not targeted: no error
    with pytest.raises(TransientToolError):
        inj.maybe_fail(scope="s", tool="calculator", ordinal=1, attempt=1)
