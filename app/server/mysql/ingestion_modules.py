"""Ingestion modules: read one ingestion module (by default super_index) from the company
database, with every column of its row.

    SELECT id, module_name, description, is_active, created_at, updated_at
    FROM ingestion_modules WHERE module_name = 'super_index'

module_name is unique, so a name matches at most one row. The connection is the SOURCE
profile (DB_* in .env) from app/auth/mysql_auth.py, whose sessions are READ ONLY: this module
only reads, and MySQL rejects any write.

Usage:
    python -m app.server.mysql.ingestion_modules                 # the super_index module
    python -m app.server.mysql.ingestion_modules other_module    # another module by name
    python -m app.server.mysql.ingestion_modules --json          # the row as JSON

From code:
    from app.server.mysql.ingestion_modules import fetch_ingestion_module
    module = fetch_ingestion_module()            # IngestionModule, or None when not found
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import pymysql
from rich.markup import escape

from app.auth import mysql_auth
from app.auth.common import AuthError, console, hint, status
from app.cloud.display import field

DEFAULT_MODULE = "super_index"
COLUMNS = ("id", "module_name", "description", "is_active", "created_at", "updated_at")
QUERY = f"SELECT {', '.join(COLUMNS)} FROM ingestion_modules WHERE module_name = %s"


@dataclass(frozen=True)
class IngestionModule:
    """One row of ingestion_modules."""

    id: str
    module_name: str
    description: str | None
    is_active: bool
    created_at: datetime | None
    updated_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        """The row as JSON-ready values (timestamps as ISO 8601)."""
        return {key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in asdict(self).items()}


def fetch_ingestion_module(module_name: str = DEFAULT_MODULE) -> IngestionModule | None:
    """Every column of the ingestion module with this name, or None when there is no such row."""
    profile = mysql_auth.SOURCE
    try:
        with mysql_auth.connect(profile) as connection, connection.cursor() as cursor:
            cursor.execute(QUERY, (module_name,))
            row = cursor.fetchone()
    except pymysql.MySQLError as exc:
        raise mysql_auth.to_auth_error(exc, profile) from exc
    if row is None:
        return None
    values = dict(zip(COLUMNS, row))
    return IngestionModule(
        id=str(values["id"]),
        module_name=values["module_name"],
        description=values["description"],
        is_active=bool(values["is_active"]),
        created_at=values["created_at"],
        updated_at=values["updated_at"],
    )


def _time(moment: datetime | None) -> str:
    return f"{moment:%Y-%m-%d %H:%M:%S}" if moment else ""


def print_module(module: IngestionModule) -> None:
    style = "green" if module.is_active else "red"
    console.print()
    console.print(f"[{style}]●[/] [bold]{escape(module.module_name)}[/] [dim]· ingestion module[/]")
    field("ID", module.id)
    field("Module name", module.module_name)
    field("Description", module.description)
    field("Active", "yes" if module.is_active else "no", style)
    field("Created", _time(module.created_at))
    field("Updated", _time(module.updated_at))
    console.print()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit code 0 when the module was found, 1 otherwise."""
    parser = argparse.ArgumentParser(
        prog="python -m app.server.mysql.ingestion_modules",
        description="Show one ingestion module from the company database (read-only).",
    )
    parser.add_argument("module_name", nargs="?", default=DEFAULT_MODULE, metavar="MODULE_NAME",
                        help=f"module_name to look up (default: {DEFAULT_MODULE})")
    parser.add_argument("-j", "--json", action="store_true", help="print the row as JSON")
    args = parser.parse_args(argv)

    try:
        module = fetch_ingestion_module(args.module_name)
    except AuthError as exc:
        status(False, f"Could not read ingestion modules: {exc}")
        hint(exc.hints)
        return 1
    if module is None:
        status(False, f"No ingestion module named '{args.module_name}'")
        hint(("Check the name: module_name must match exactly",))
        return 1

    if args.json:
        print(json.dumps(module.to_dict()))
    else:
        print_module(module)
    return 0


if __name__ == "__main__":
    sys.exit(main())
