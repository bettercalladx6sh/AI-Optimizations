"""Analysis database commands.

Usage:
    python -m app.db status     # migrations applied/pending and row counts per table
    python -m app.db migrate    # apply pending migrations now (runs also apply them automatically)
"""

from __future__ import annotations

import argparse

from app.auth.common import AuthError, console, hint, status, warning
from app.cloud.display import section
from app.db.database import applied_migrations, available_migrations, connection, migrate


def _print_status() -> None:
    with connection() as conn:
        applied = applied_migrations(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT table_name, table_rows FROM information_schema.tables "
                "WHERE table_schema = DATABASE() ORDER BY table_name"
            )
            tables = cursor.fetchall()
            counts = {}
            for (name, _) in tables:
                cursor.execute(f"SELECT COUNT(*) FROM `{name}`")
                counts[name] = cursor.fetchone()[0]

    migrations = available_migrations()
    pending = [m for m in migrations if m.version not in applied]
    console.print()
    console.print(f"[bold]Analysis database[/] [dim]·[/] {len(migrations)} migrations [dim]·[/] {len(pending)} pending")
    console.print()

    section("Migrations", indent=0)
    for migration in migrations:
        done = applied.get(migration.version)
        if done:
            console.print(f"  [green]✔[/] {migration.path.name}  [dim]applied {done.applied_at:%Y-%m-%d %H:%M} UTC[/]")
        else:
            console.print(f"  [yellow]⚠[/] {migration.path.name}  [yellow]pending[/]")
    console.print()

    section("Tables", indent=0)
    width = max((len(name) for name in counts), default=0) + 2
    for name, count in counts.items():
        console.print(f"  [dim]{name:<{width}}[/]{count} row{'s' if count != 1 else ''}")
    console.print()

    if pending:
        warning("Pending migrations are applied by the next analysis run, or now with: python -m app.db migrate")


def _migrate() -> None:
    with connection() as conn:
        newly_applied = migrate(conn)
    if not newly_applied:
        status(True, "Schema is up to date; no migrations to apply")
    for migration in newly_applied:
        status(True, f"Applied {migration.path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db", description="Analysis database commands.")
    parser.add_argument("command", choices=("status", "migrate"), help="status: show schema state; migrate: apply pending migrations")
    args = parser.parse_args()

    try:
        if args.command == "status":
            _print_status()
        else:
            _migrate()
    except AuthError as exc:
        status(False, f"Database {args.command} failed: {exc}")
        hint(exc.hints)
        return 1
    return 0


raise SystemExit(main())