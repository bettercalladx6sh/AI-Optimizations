"""MySQL authentication: the single entry point for every database connection in the app.

Two connection profiles, both read from .env:
    SOURCE    DB_*           the company UAT database; every session is read-only
    ANALYSIS  ANALYSIS_DB_*  the local analysis store (docker compose); read-write
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pymysql
from pymysql.connections import Connection

from app.auth.common import ENV_PATH, AuthError, read_env, report_failure, status

CONNECT_TIMEOUT = 10

# Hints use {prefix} so they name the right variables for each profile.
ERROR_HINTS: dict[int, tuple[str, ...]] = {
    1044: ("Grant the user access to {prefix}NAME",),
    1045: (
        "Check {prefix}USER and {prefix}PASSWORD",
        "Check the user is allowed to connect from this machine's IP",
    ),
    1049: ("Check that {prefix}NAME exists on the server",),
    1792: ("This connection is read-only; writes are blocked on purpose",),
    2003: ("Check {prefix}HOST and {prefix}PORT",),
    2005: ("Check {prefix}HOST spelling and DNS resolution",),
    3159: ("Server requires TLS: set {prefix}SSL_CA to the server's CA certificate path",),
}


@dataclass(frozen=True)
class MySQLProfile:
    """One set of connection settings in .env, identified by its variable prefix."""

    name: str
    prefix: str
    label: str
    read_only: bool
    unreachable_hints: tuple[str, ...]


SOURCE = MySQLProfile(
    name="source",
    prefix="DB_",
    label="UAT database",
    read_only=True,
    unreachable_hints=(
        "Check the firewall / security group allows your IP",
        "Connect to the VPN if the UAT server is private",
    ),
)
ANALYSIS = MySQLProfile(
    name="analysis",
    prefix="ANALYSIS_DB_",
    label="analysis store",
    read_only=False,
    unreachable_hints=("Start the local analysis database: docker compose up -d",),
)
PROFILES = {profile.name: profile for profile in (SOURCE, ANALYSIS)}


class MySQLAuthError(AuthError):
    """Raised when a database is unreachable or rejects the credentials."""


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


def load_settings(profile: MySQLProfile = SOURCE) -> MySQLSettings:
    """Read and validate one profile's settings from .env."""
    p = profile.prefix
    env = read_env(
        (f"{p}HOST", f"{p}PORT", f"{p}USER", f"{p}PASSWORD", f"{p}NAME"),
        MySQLAuthError,
        optional={f"{p}SSL_CA": ""},
    )

    try:
        port = int(env[f"{p}PORT"])
    except ValueError:
        raise MySQLAuthError(f"{p}PORT must be a number, got '{env[f'{p}PORT']}'") from None

    return MySQLSettings(
        host=env[f"{p}HOST"],
        port=port,
        user=env[f"{p}USER"],
        password=env[f"{p}PASSWORD"],
        database=env[f"{p}NAME"],
        ssl_ca=env[f"{p}SSL_CA"],
    )


def to_auth_error(exc: pymysql.MySQLError, profile: MySQLProfile) -> MySQLAuthError:
    """Convert a PyMySQL error into MySQLAuthError with fix hints for the profile."""
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    message = str(exc.args[1]) if len(exc.args) > 1 else str(exc)
    hints = [hint.format(prefix=profile.prefix) for hint in ERROR_HINTS.get(code, ())]
    if code == 2003:
        hints.extend(profile.unreachable_hints)
    return MySQLAuthError(f"[{code}] {message}" if code else message, hints)


@contextmanager
def connect(profile: MySQLProfile = SOURCE, **kwargs: Any) -> Iterator[Connection]:
    """Open a connection for a profile and always close it, e.g. `with connect(ANALYSIS) as conn:`.

    SOURCE sessions are set to READ ONLY, so MySQL rejects any write to the company database.
    """
    settings = load_settings(profile)

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
        if profile.read_only:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
    except pymysql.MySQLError as exc:
        raise to_auth_error(exc, profile) from exc

    try:
        yield connection
    finally:
        connection.close()


def get_identity(profile: MySQLProfile = SOURCE) -> MySQLIdentity:
    """Connect and confirm the logged-in user, server version and database."""
    try:
        with connect(profile) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT CURRENT_USER(), VERSION(), DATABASE()")
            user, version, database = cursor.fetchone()
    except pymysql.MySQLError as exc:
        raise to_auth_error(exc, profile) from exc

    return MySQLIdentity(user=user, version=version, database=database)


def authenticate(profile: MySQLProfile = SOURCE) -> MySQLIdentity:
    """Run the full connection check for a profile and print each step."""
    try:
        settings = load_settings(profile)
    except MySQLAuthError as exc:
        report_failure("Verifying .env", exc)
        raise
    status(True, f"Verifying .env ({ENV_PATH})")

    status(
        True,
        f"Host [{settings.host}:{settings.port}]  User [{settings.user}]  "
        f"Password [********]  Database [{settings.database}]  "
        f"TLS [{'on' if settings.ssl_ca else 'off'}]  "
        f"Mode [{'read-only' if profile.read_only else 'read-write'}]",
    )

    try:
        identity = get_identity(profile)
    except MySQLAuthError as exc:
        report_failure("Connection", exc)
        raise
    status(
        True,
        f"Name: {identity.user} on MySQL {identity.version} "
        f"(database {identity.database}, {profile.label})",
    )
    status(True, "Authentication successful")

    return identity


def main(profile: MySQLProfile = SOURCE) -> int:
    """CLI entry point: exit code 0 on success, 1 on failure."""
    try:
        authenticate(profile)
    except MySQLAuthError:
        return 1
    return 0