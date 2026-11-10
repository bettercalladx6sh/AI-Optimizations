"""Backoff for AWS API calls that are throttled or briefly unavailable.

botocore already retries a few times inside each call; with_backoff adds a longer, jittered
outer layer (app/cloud/backoff.py) so a burst of requests (many families, many pages) slows
down instead of failing. Only throttling, 5xx and connection errors are retried; permission or
input errors fail at once.

Usage:
    response = with_backoff(client.get_products, ServiceCode="AmazonEC2", Filters=filters)
"""

from __future__ import annotations

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

from app.cloud.backoff import MAX_ATTEMPTS, backoff_delay, retry

__all__ = ["MAX_ATTEMPTS", "backoff_delay", "is_retryable", "with_backoff"]

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


def _describe(exc: BaseException) -> str:
    return exc.response.get("Error", {}).get("Code", "error") if isinstance(exc, ClientError) else type(exc).__name__


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
    return retry(call, *args, retryable=is_retryable, describe=_describe,
                 label=label or getattr(call, "__name__", "AWS call"), attempts=attempts, sleep=sleep, **kwargs)
