"""Authentication package: shared cloud, database and search connectivity."""

from app.auth import aws_auth, azure_auth, mysql_auth, opensearch_auth
from app.auth.common import AuthError

__all__ = ["AuthError", "aws_auth", "azure_auth", "mysql_auth", "opensearch_auth"]