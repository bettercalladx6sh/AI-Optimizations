"""MySQL authentication: the single entry point for every database connection in the app."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pymysql
from pymysql.connections import Connection

from app.auth.common import ENV_PATH, AuthError, read_env, report_failure, status

REQUIRED_VARS = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")
OPTIONAL_VARS = {"DB_SSL_CA": ""}

CONNECT_TIMEOUT = 10

ERROR_HINTS: dict[int, tuple[str, ...]] = {
    1044: ("Grant the user access to DB_NAME",),
    1045: (
        "Check DB_USER and DB_PASSWORD",
        "Check the user is allowed to connect from this machine's IP",
    ),
    1049: ("Check that DB_NAME exists on the server",),
    2003: (
        "Check DB_HOST and DB_PORT",
        "Check the firewall / security group allows your IP",
        "Connect to the VPN if the UAT server is private",
    ),
    2005: ("Check DB_HOST spelling and DNS resolution",),
    3159: ("Server requires TLS: set DB_SSL_CA to the server's CA certificate path",),
}


class MySQLAuthError(AuthError):
    """Raised when the database is unreachable or rejects the credentials."""


@dataclass(frozen=True)
class MySQLSettings:
    """Validated connection settings from .env."""

    host: str
    port: int
    user: str
    password: str
    database: str
    ssl_ca: str


@dataclass(frozen=True)
class MySQLIdentity:
    """Connection details confirmed by the server."""

    user: str
    version: str
    database: str


def load_settings() -> MySQLSettings:
    """Read and validate the database settings from .env."""
    env = read_env(REQUIRED_VARS, MySQLAuthError, optional=OPTIONAL_VARS)

    try:
        port = int(env["DB_PORT"])
    except ValueError:
        raise MySQLAuthError(f"DB_PORT must be a number, got '{env['DB_PORT']}'") from None

    return MySQLSettings(
        host=env["DB_HOST"],
        port=port,
        user=env["DB_USER"],
        password=env["DB_PASSWORD"],
        database=env["DB_NAME"],
        ssl_ca=env["DB_SSL_CA"],
    )


def _to_auth_error(exc: pymysql.MySQLError) -> MySQLAuthError:
    """Convert a PyMySQL error into MySQLAuthError with fix hints."""
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    message = str(exc.args[1]) if len(exc.args) > 1 else str(exc)
    text = f"[{code}] {message}" if code else message
    return MySQLAuthError(text, ERROR_HINTS.get(code, ()))


@contextmanager
def connect(**kwargs: Any) -> Iterator[Connection]:
    """Open a database connection and always close it, e.g. `with connect() as conn:`."""
    settings = load_settings()

    try:
        connection = pymysql.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            database=settings.database,
            ssl={"ca": settings.ssl_ca} if settings.ssl_ca else None,
            connect_timeout=CONNECT_TIMEOUT,
            charset="utf8mb4",
            **kwargs,
        )
    except pymysql.MySQLError as exc:
        raise _to_auth_error(exc) from exc

    try:
        yield connection
    finally:
        connection.close()


def get_identity() -> MySQLIdentity:
    """Connect and confirm the logged-in user, server version and database."""
    try:
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT CURRENT_USER(), VERSION(), DATABASE()")
            user, version, database = cursor.fetchone()
    except pymysql.MySQLError as exc:
        raise _to_auth_error(exc) from exc

    return MySQLIdentity(user=user, version=version, database=database)


def authenticate() -> MySQLIdentity:
    """Run the full connection check and print the status of each step."""
    try:
        settings = load_settings()
    except MySQLAuthError as exc:
        report_failure("Verifying .env", exc)
        raise
    status(True, f"Verifying .env ({ENV_PATH})")

    status(
        True,
        f"Host [{settings.host}:{settings.port}]  User [{settings.user}]  "
        f"Password [********]  Database [{settings.database}]  "
        f"TLS [{'on' if settings.ssl_ca else 'off'}]",
    )

    try:
        identity = get_identity()
    except MySQLAuthError as exc:
        report_failure("Connection", exc)
        raise
    status(True, f"Name: {identity.user} on MySQL {identity.version} (database {identity.database})")
    status(True, "Authentication successful")

    return identity


def main() -> int:
    """CLI entry point: exit code 0 on success, 1 on failure."""
    try:
        authenticate()
    except MySQLAuthError:
        return 1
    return 0