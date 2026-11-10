"""Exponential backoff with full jitter for cloud API calls that are throttled or briefly unavailable.

Provider-neutral: each provider says which errors are retryable (app/cloud/aws/backoff.py,
app/cloud/azure/rest.py). Only throttling, 5xx and connection errors should be retried;
permission or input errors fail at once.

    delay for attempt n = random(0, min(MAX_DELAY, BASE_DELAY x 2^(n-1)))

When the provider says how long to wait (e.g. a Retry-After header), the delay is at least
that long, capped at MAX_DELAY.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from app.auth.common import console

MAX_ATTEMPTS = 6
BASE_DELAY = 0.5      # seconds
MAX_DELAY = 20.0      # seconds

T = TypeVar("T")


def backoff_delay(attempt: int, base: float = BASE_DELAY, cap: float = MAX_DELAY) -> float:
    """Full-jitter delay before retry number `attempt` (1-based)."""
    return random.uniform(0.0, min(cap, base * 2 ** (attempt - 1)))


def retry(
    call: Callable[..., T],
    *args: object,
    retryable: Callable[[BaseException], bool],
    describe: Callable[[BaseException], str] = lambda exc: type(exc).__name__,
    wait_hint: Callable[[BaseException], float | None] = lambda exc: None,
    label: str | None = None,
    attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: object,
) -> T:
    """Call `call(*args, **kwargs)`, retrying errors for which `retryable` is true.

    The last error is re-raised unchanged, so callers keep their usual error mapping.
    """
    label = label or getattr(call, "__name__", "API call")
    for attempt in range(1, attempts + 1):
        try:
            return call(*args, **kwargs)
        except Exception as exc:
            if attempt == attempts or not retryable(exc):
                raise
            delay = max(backoff_delay(attempt), min(wait_hint(exc) or 0.0, MAX_DELAY))
            console.print(f"  [yellow]↻[/] [dim]{label}: {describe(exc)}; retry {attempt}/{attempts - 1} in {delay:.1f}s[/]")
            sleep(delay)
    raise AssertionError("unreachable")
