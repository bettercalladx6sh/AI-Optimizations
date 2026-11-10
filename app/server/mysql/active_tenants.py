"""Active tenants: the IDs of every tenant that is active and not deleted, read from the
company database.

    SELECT id, company_name FROM tenants WHERE tenant_status = 'active' AND is_deleted = 0

The connection is the SOURCE profile (DB_* in .env) from app/auth/mysql_auth.py, whose
sessions are READ ONLY: this module only reads, and MySQL rejects any write. Only the ID and
company name are selected; other columns (credentials, endpoints) are never read.

Usage:
    python -m app.server.mysql.active_tenants          # list active tenants
    python -m app.server.mysql.active_tenants --json   # tenant IDs as a JSON array

From code:
    from app.server.mysql.active_tenants import active_tenant_ids
    ids = active_tenant_ids()
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

import pymysql
from rich.markup import escape

from app.auth import mysql_auth
from app.auth.common import AuthError, console, hint, status
from app.cloud.display import plural

ACTIVE_STATUS = "active"
QUERY = (
    "SELECT id, company_name FROM tenants "
    "WHERE tenant_status = %s AND is_deleted = 0 "
    "ORDER BY company_name, id"
)


@dataclass(frozen=True)
class Tenant:
    """One active tenant."""

    tenant_id: str
    company_name: str


def fetch_active_tenants() -> list[Tenant]:
    """Every tenant with tenant_status = 'active' and is_deleted = 0, ordered by company name."""
    profile = mysql_auth.SOURCE
    try:
        with mysql_auth.connect(profile) as connection, connection.cursor() as cursor:
            cursor.execute(QUERY, (ACTIVE_STATUS,))
            rows = cursor.fetchall()
    except pymysql.MySQLError as exc:
        raise mysql_auth.to_auth_error(exc, profile) from exc
    return [Tenant(tenant_id=str(tenant_id), company_name=company_name or "") for tenant_id, company_name in rows]


def active_tenant_ids() -> list[str]:
    """The IDs of every active, non-deleted tenant."""
    return [tenant.tenant_id for tenant in fetch_active_tenants()]


def print_tenants(tenants: list[Tenant]) -> None:
    console.print()
    console.print(f"[bold]Active tenants[/] [dim]·[/] {plural(len(tenants), 'tenant')} found "
                  f"[dim](tenant_status = '{ACTIVE_STATUS}', is_deleted = 0)[/]")
    console.print()
    width = max((len(tenant.company_name) for tenant in tenants), default=0)
    for tenant in tenants:
        console.print(f"  [green]●[/] {escape(tenant.company_name):<{width}}  [dim]{tenant.tenant_id}[/]",
                      highlight=False)
    if tenants:
        console.print()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit code 0 on success, 1 on failure."""
    parser = argparse.ArgumentParser(
        prog="python -m app.server.mysql.active_tenants",
        description="List the active, non-deleted tenants from the company database (read-only).",
    )
    parser.add_argument("-j", "--json", action="store_true", help="print only the tenant IDs as a JSON array")
    args = parser.parse_args(argv)

    try:
        tenants = fetch_active_tenants()
    except AuthError as exc:
        status(False, f"Could not read active tenants: {exc}")
        hint(exc.hints)
        return 1

    if args.json:
        print(json.dumps([tenant.tenant_id for tenant in tenants]))
    else:
        print_tenants(tenants)
    return 0


if __name__ == "__main__":
    sys.exit(main())
