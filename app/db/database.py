"""Analysis database: sessions and schema migrations.

Connects only to the local analysis database (mysql_auth.ANALYSIS, started with docker
compose); the company database is never written to.

The schema lives in numbered SQL files in app/db/migrations/ (0001_initial_schema.sql,
0002_..., ...). Pending files are applied in order the first time a session opens, and
each applied file is recorded in schema_migrations. To store new details later, add a
new numbered file; never edit one that has already been applied.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pymysql
from pymysql.connections import Connection

from app.auth import mysql_auth
from app.auth.common import AuthError

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
LOCK_NAME = "cost_optimizer_schema_migrations"
LOCK_TIMEOUT = 30

MIGRATIONS_TABLE = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version    INT UNSIGNED PRIMARY KEY,
        name       VARCHAR(255) NOT NULL,
        checksum   CHAR(64)     NOT NULL,
        applied_at DATETIME(6)  NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_schema_ready = False


class DatabaseError(AuthError):
    """Raised when the analysis database schema cannot be brought up to date."""


@dataclass(frozen=True)
class Migration:
    """One numbered SQL file in app/db/migrations/."""

    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def statements(self) -> list[str]:
        """Split the file into statements, dropping '--' comment lines."""
        lines = [line for line in self.sql.splitlines() if not line.lstrip().startswith("--")]
        return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]


@dataclass(frozen=True)
class AppliedMigration:
    """A migration recorded in schema_migrations."""

    version: int
    name: str
    checksum: str
    applied_at: datetime


def available_migrations() -> list[Migration]:
    """Return the migration files in version order."""
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = MIGRATION_FILE.match(path.name)
        if not match:
            raise DatabaseError(f"migration file name must look like 0001_name.sql: {path.name}")
        migrations.append(Migration(int(match[1]), match[2], path))
    return migrations


def applied_migrations(connection: Connection) -> dict[int, AppliedMigration]:
    """Return the migrations already applied to this database."""
    with connection.cursor() as cursor:
        cursor.execute(MIGRATIONS_TABLE)
        cursor.execute("SELECT version, name, checksum, applied_at FROM schema_migrations")
        return {
            version: AppliedMigration(version, name, checksum, applied_at.replace(tzinfo=timezone.utc))
            for version, name, checksum, applied_at in cursor.fetchall()
        }


def migrate(connection: Connection) -> list[Migration]:
    """Apply every pending migration in order and return the ones applied.

    A database-wide lock stops two runs from migrating at the same time.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT GET_LOCK(%s, %s)", (LOCK_NAME, LOCK_TIMEOUT))
        (locked,) = cursor.fetchone()
    if not locked:
        raise DatabaseError("another process is migrating the analysis database; try again shortly")

    try:
        applied = applied_migrations(connection)
        newly_applied = []
        for migration in available_migrations():
            done = applied.get(migration.version)
            if done:
                if done.checksum != migration.checksum:
                    raise DatabaseError(
                        f"migration {migration.path.name} was edited after it was applied",
                        ("Revert the edit and add a new numbered migration file instead",),
                    )
                continue
            with connection.cursor() as cursor:
                for statement in migration.statements():
                    cursor.execute(statement)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (%s, %s, %s, %s)",
                    (migration.version, migration.name, migration.checksum,
                     datetime.now(timezone.utc).replace(tzinfo=None)),
                )
            connection.commit()
            newly_applied.append(migration)
        return newly_applied
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))


@contextmanager
def connection() -> Iterator[Connection]:
    """Open a raw analysis-database connection, converting MySQL errors to AuthError."""
    with mysql_auth.connect(mysql_auth.ANALYSIS) as conn:
        try:
            yield conn
        except pymysql.MySQLError as exc:
            raise mysql_auth.to_auth_error(exc, mysql_auth.ANALYSIS) from exc


@contextmanager
def session() -> Iterator[Connection]:
    """Open the analysis database with an up-to-date schema; commit the block, or roll back on error."""
    global _schema_ready
    with connection() as conn:
        if not _schema_ready:
            migrate(conn)
            _schema_ready = True
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise