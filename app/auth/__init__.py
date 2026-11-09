"""Authentication package: shared cloud provider connectivity."""

from app.auth.aws_auth import (
    AWSAuthError,
    AWSIdentity,
    authenticate,
    get_client,
    get_identity,
    get_session,
)

__all__ = [
    "AWSAuthError",
    "AWSIdentity",
    "authenticate",
    "get_client",
    "get_identity",
    "get_session",
]