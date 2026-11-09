"""Shared error handling for every AWS API call made by the discovery modules."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from botocore.exceptions import BotoCoreError, ClientError

from app.auth.common import AuthError

PERMISSION_CODES = {"UnauthorizedOperation", "AccessDenied", "AccessDeniedException"}


class AwsApiError(AuthError):
    """Raised when an AWS API call fails (permissions, bad input, region or network)."""


@contextmanager
def aws_errors(
    action: str,
    hints_by_code: Mapping[str, tuple[str, ...]] | None = None,
) -> Iterator[None]:
    """Turn boto errors raised inside the block into AwsApiError with fix hints.

    `action` is the IAM action being called, e.g. 'ec2:DescribeInstances'; it is
    named in the hint when the identity lacks permission.
    """
    try:
        yield
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = error.get("Code", "")
        if code in PERMISSION_CODES:
            hints: tuple[str, ...] = (f"Grant the IAM identity the {action} permission",)
        else:
            hints = (hints_by_code or {}).get(code, ())
        raise AwsApiError(error.get("Message", str(exc)), hints) from exc
    except BotoCoreError as exc:
        raise AwsApiError(str(exc)) from exc