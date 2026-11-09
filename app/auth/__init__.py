"""Authentication package: shared cloud and database connectivity."""

from app.auth import aws_auth, azure_auth, mysql_auth
from app.auth.common import AuthError

__all__ = ["AuthError", "aws_auth", "azure_auth", "mysql_auth"]