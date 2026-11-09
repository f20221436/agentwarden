"""Retry with exponential backoff and full jitter, bounded by the execution deadline.

Full jitter (AWS Architecture Blog, "Exponential Backoff and Jitter"):
    sleep = uniform(0, min(cap, base * 2**attempt))
It spreads retries out so many clients failing together do not retry in lock-step.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from agentwarden.config import RetryPolicy
from agentwarden.reliability.errors import is_transient

T = TypeVar("T")


def backoff_delay(attempt: int, policy: RetryPolicy, rng: random.Random) -> float:
    """Delay before retry number ``attempt`` (1-based)."""
    ceiling = min(policy.max_delay_s, policy.base_delay_s * (2 ** (attempt - 1)))
    return rng.uniform(0, ceiling)


@dataclass
class RetryOutcome:
    attempts: int = 0
    errors: list[str] = field(default_factory=list)  # one entry per failed attempt
    delays: list[float] = field(default_factory=list)


def call_with_retry(
    fn: Callable[[int], T],
    *,
    policy: RetryPolicy,
    retryable: bool,
    deadline: float,
    outcome: RetryOutcome,
    rng: random.Random | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call ``fn(attempt_no)``; retry only transient errors, only if ``retryable`` and policy enabled.

    ``retryable`` is False for non-idempotent operations: they are never retried blindly.
    Never sleeps past ``deadline`` (a ``time.monotonic()`` value).
    """
    rng = rng or random.Random()
    max_attempts = policy.max_attempts if (policy.enabled and retryable) else 1
    attempt = 0
    while True:
        attempt += 1
        outcome.attempts = attempt
        try:
            return fn(attempt)
        except Exception as exc:
            outcome.errors.append(f"{type(exc).__name__}: {exc}")
            if attempt >= max_attempts or not is_transient(exc):
                raise
            delay = backoff_delay(attempt, policy, rng)
            if clock() + delay >= deadline:
                raise
            outcome.delays.append(delay)
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)
