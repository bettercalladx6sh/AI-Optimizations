"""Exponential backoff with full jitter for AWS API calls that are throttled or briefly unavailable.

botocore already retries a few times inside each call; with_backoff adds a longer, jittered
outer layer so a burst of requests (many families, many pages) slows down instead of failing.
Only throttling, 5xx and connection errors are retried; permission or input errors fail at once.

    delay for attempt n = random(0, min(MAX_DELAY, BASE_DELAY x 2^(n-1)))

Usage:
    response = with_backoff(client.get_products, ServiceCode="AmazonEC2", Filters=filters)
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from app.auth.common import console

MAX_ATTEMPTS = 6
BASE_DELAY = 0.5      # seconds
MAX_DELAY = 20.0      # seconds

THROTTLING_CODES = {
    "Throttling",
    "ThrottlingException",
    "ThrottledException",
    "RequestThrottled",
    "RequestThrottledException",
    "RequestLimitExceeded",
    "TooManyRequestsException",
    "ProvisionedThroughputExceededException",
    "SlowDown",
    "PriorRequestNotComplete",
}
TRANSIENT_CODES = {"InternalError", "InternalFailure", "InternalServerError", "ServiceUnavailable"}
CONNECTION_ERRORS = (EndpointConnectionError, ConnectionClosedError, ConnectTimeoutError, ReadTimeoutError)

T = TypeVar("T")


def is_retryable(exc: BaseException) -> bool:
    """True for throttling, server-side (5xx) and connection errors."""
    if isinstance(exc, CONNECTION_ERRORS):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return code in THROTTLING_CODES or code in TRANSIENT_CODES or http_status == 429 or http_status >= 500
    return False


def backoff_delay(attempt: int, base: float = BASE_DELAY, cap: float = MAX_DELAY) -> float:
    """Full-jitter delay before retry number `attempt` (1-based)."""
    return random.uniform(0.0, min(cap, base * 2 ** (attempt - 1)))


def _report_retry(label: str, exc: BaseException, attempt: int, retries: int, delay: float) -> None:
    reason = exc.response.get("Error", {}).get("Code", "error") if isinstance(exc, ClientError) else type(exc).__name__
    console.print(f"  [yellow]↻[/] [dim]{label}: {reason}; retry {attempt}/{retries} in {delay:.1f}s[/]")


def with_backoff(
    call: Callable[..., T],
    *args: object,
    label: str | None = None,
    attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: object,
) -> T:
    """Call `call(*args, **kwargs)`, retrying retryable AWS errors with exponential backoff and jitter.

    The last error is re-raised unchanged, so callers keep their usual error mapping
    (for example app.cloud.aws.errors.aws_errors).
    """
    label = label or getattr(call, "__name__", "AWS call")
    for attempt in range(1, attempts + 1):
        try:
            return call(*args, **kwargs)
        except Exception as exc:
            if attempt == attempts or not is_retryable(exc):
                raise
            delay = backoff_delay(attempt)
            _report_retry(label, exc, attempt, attempts - 1, delay)
            sleep(delay)
    raise AssertionError("unreachable")