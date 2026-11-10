"""Data ingestion audit: the completed super_index ingestion runs of 2026 for every active
tenant, read from the company database.

    1. active tenants         tenants with tenant_status = 'active' and is_deleted = 0
                              (active_tenants.py)
    2. super_index module ID  ingestion_modules where module_name = 'super_index'
                              (ingestion_modules.py)
    3. audit rows             data_ingestion_audit where
                                  tenant_id  IN (active tenant IDs)
                                  audit_date in 2026 (2026-01-01 <= audit_date < 2027-01-01)
                                  status     = 'COMPLETED'
                                  module_id  = super_index module ID

Every column of each matching row is returned. The year is matched as a date range, so the
query compares audit_date directly instead of wrapping it in YEAR(). All three reads use the
SOURCE profile (DB_* in .env) from app/auth/mysql_auth.py, whose sessions are READ ONLY: this
module only reads, and MySQL rejects any write.

Usage:
    python -m app.server.mysql.data_ingestion_audit                  # 2026, COMPLETED, super_index
    python -m app.server.mysql.data_ingestion_audit --year 2025      # another year
    python -m app.server.mysql.data_ingestion_audit --json           # the rows as JSON

From code:
    from app.server.mysql.data_ingestion_audit import fetch_audits
    result = fetch_audits()                      # AuditResult: module, tenants, audits
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

import pymysql
from rich.markup import escape

from app.auth import mysql_auth
from app.auth.common import AuthError, console, hint, status
from app.cloud.display import NOT_SET, format_duration, plural
from app.server.mysql.active_tenants import Tenant, fetch_active_tenants
from app.server.mysql.ingestion_modules import DEFAULT_MODULE, IngestionModule, fetch_ingestion_module

DEFAULT_YEAR = 2026
DEFAULT_STATUS = "COMPLETED"
COLUMNS = ("id", "tenant_id", "audit_date", "status", "start_time", "end_time", "job_id", "module_id")


class AuditError(AuthError):
    """Raised when the audit filter cannot be built (e.g. the ingestion module does not exist)."""


@dataclass(frozen=True)
class Audit:
    """One row of data_ingestion_audit."""

    id: str
    tenant_id: str
    audit_date: date | None
    status: str | None
    start_time: datetime | None
    end_time: datetime | None
    job_id: str | None
    module_id: str

    def to_dict(self) -> dict[str, Any]:
        """The row as JSON-ready values (dates and timestamps as ISO 8601)."""
        return {key: value.isoformat() if isinstance(value, (date, datetime)) else value
                for key, value in asdict(self).items()}


@dataclass(frozen=True)
class AuditResult:
    """The filter that was applied and the rows it matched."""

    module: IngestionModule
    tenants: list[Tenant]
    year: int
    status: str
    audits: list[Audit]


def _query(tenant_count: int) -> str:
    placeholders = ", ".join(["%s"] * tenant_count)
    return (
        f"SELECT {', '.join(COLUMNS)} FROM data_ingestion_audit "
        f"WHERE tenant_id IN ({placeholders}) "
        "AND audit_date >= %s AND audit_date < %s "
        "AND status = %s AND module_id = %s "
        "ORDER BY tenant_id, audit_date, start_time"
    )


def _to_audit(row: tuple[Any, ...]) -> Audit:
    values = dict(zip(COLUMNS, row))
    for key in ("id", "tenant_id", "job_id", "module_id"):
        if values[key] is not None:
            values[key] = str(values[key])
    return Audit(**values)


def fetch_audits(
    year: int = DEFAULT_YEAR,
    audit_status: str = DEFAULT_STATUS,
    module_name: str = DEFAULT_MODULE,
) -> AuditResult:
    """Audit rows of the active tenants for one module, year and status (read-only)."""
    tenants = fetch_active_tenants()
    module = fetch_ingestion_module(module_name)
    if module is None:
        raise AuditError(f"no ingestion module named '{module_name}'",
                         ("Check the name in ingestion_modules.module_name",))
    if not tenants:
        return AuditResult(module, tenants, year, audit_status, [])

    profile = mysql_auth.SOURCE
    params = (*(tenant.tenant_id for tenant in tenants), date(year, 1, 1), date(year + 1, 1, 1),
              audit_status, module.id)
    try:
        with mysql_auth.connect(profile) as connection, connection.cursor() as cursor:
            cursor.execute(_query(len(tenants)), params)
            rows = cursor.fetchall()
    except pymysql.MySQLError as exc:
        raise mysql_auth.to_auth_error(exc, profile) from exc
    return AuditResult(module, tenants, year, audit_status, [_to_audit(row) for row in rows])


# ---------------------------------------------------------------- output


def _time(moment: datetime | None) -> str:
    return f"{moment:%Y-%m-%d %H:%M:%S}" if moment else NOT_SET


def _took(audit: Audit) -> str:
    """Run time, e.g. '42 s' or '5 h 10 min'; a dash when a time is missing."""
    if not (audit.start_time and audit.end_time and audit.end_time >= audit.start_time):
        return NOT_SET
    elapsed = audit.end_time - audit.start_time
    return f"{int(elapsed.total_seconds())} s" if elapsed.total_seconds() < 60 else format_duration(elapsed)


def print_result(result: AuditResult) -> None:
    per_tenant = Counter(audit.tenant_id for audit in result.audits)
    console.print()
    console.print(f"[bold]Data ingestion audit[/] [dim]·[/] {escape(result.module.module_name)} "
                  f"[dim]·[/] {result.year} [dim]·[/] {escape(result.status)} [dim]·[/] "
                  f"{plural(len(result.audits), 'row')} for {len(per_tenant)} of "
                  f"{plural(len(result.tenants), 'active tenant')}")
    console.print(f"[dim]  module_id {result.module.id}[/]")
    console.print()

    by_tenant: dict[str, list[Audit]] = {}
    for audit in result.audits:
        by_tenant.setdefault(audit.tenant_id, []).append(audit)
    for tenant in result.tenants:
        audits = by_tenant.get(tenant.tenant_id, [])
        style = "green" if audits else "yellow"
        console.print(f"[{style}]●[/] [bold]{escape(tenant.company_name)}[/] [dim]· {tenant.tenant_id} · "
                      f"{plural(len(audits), 'row')}[/]", highlight=False)
        for audit in audits:
            console.print(
                f"    {audit.audit_date or NOT_SET}  {_time(audit.start_time)} → {_time(audit.end_time)}  "
                f"[dim]took {_took(audit)} · job {audit.job_id or NOT_SET} · id {audit.id}[/]",
                highlight=False,
            )
        console.print()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit code 0 on success (even with no matching rows), 1 on failure."""
    parser = argparse.ArgumentParser(
        prog="python -m app.server.mysql.data_ingestion_audit",
        description="Show data ingestion audit rows of the active tenants (read-only).",
    )
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR, help=f"audit_date year (default: {DEFAULT_YEAR})")
    parser.add_argument("--status", default=DEFAULT_STATUS, help=f"audit status (default: {DEFAULT_STATUS})")
    parser.add_argument("--module", default=DEFAULT_MODULE, help=f"ingestion module name (default: {DEFAULT_MODULE})")
    parser.add_argument("-j", "--json", action="store_true", help="print the matching rows as JSON")
    args = parser.parse_args(argv)

    try:
        result = fetch_audits(args.year, args.status, args.module)
    except AuthError as exc:
        status(False, f"Could not read data ingestion audits: {exc}")
        hint(exc.hints)
        return 1

    if args.json:
        print(json.dumps([audit.to_dict() for audit in result.audits]))
    else:
        print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())