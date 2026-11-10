"""Completed audits, newest first: the data ingestion audit rows of data_ingestion_audit.py
(active tenants, super_index module, audit_date in 2026, status COMPLETED), ordered from the
most recently completed to the oldest.

Order: audit_date descending; rows of the same date by end_time, then start_time, descending;
rows without an audit_date come last. The rows are read once by fetch_audits() (read-only,
company database) and only re-ordered here, so both commands always use the same filter.

Usage:
    python -m app.server.mysql.completed_audits_latest              # newest to oldest
    python -m app.server.mysql.completed_audits_latest -n 20        # only the 20 most recent
    python -m app.server.mysql.completed_audits_latest --year 2025  # another year
    python -m app.server.mysql.completed_audits_latest --json       # the ordered rows as JSON

From code:
    from app.server.mysql.completed_audits_latest import latest_audits
    audits = latest_audits()                     # list[Audit], newest first
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

from rich.markup import escape

from app.auth.common import AuthError, console, hint, status
from app.cloud.display import NOT_SET, plural
from app.server.mysql.data_ingestion_audit import (
    DEFAULT_STATUS,
    DEFAULT_YEAR,
    Audit,
    AuditResult,
    fetch_audits,
)
from app.server.mysql.ingestion_modules import DEFAULT_MODULE


def newest_first(audits: list[Audit]) -> list[Audit]:
    """Most recently completed first: audit_date, then end_time, then start_time, all descending."""
    def key(audit: Audit) -> tuple[bool, date, datetime, datetime]:
        return (audit.audit_date is not None, audit.audit_date or date.min,
                audit.end_time or datetime.min, audit.start_time or datetime.min)
    return sorted(audits, key=key, reverse=True)


def latest_audits(
    year: int = DEFAULT_YEAR,
    audit_status: str = DEFAULT_STATUS,
    module_name: str = DEFAULT_MODULE,
    limit: int | None = None,
) -> list[Audit]:
    """The audit rows of fetch_audits(), newest first (at most `limit` rows when given)."""
    ordered = newest_first(fetch_audits(year, audit_status, module_name).audits)
    return ordered[:limit] if limit else ordered


def _time(moment: datetime | None) -> str:
    return f"{moment:%Y-%m-%d %H:%M:%S}" if moment else NOT_SET


def print_latest(result: AuditResult, audits: list[Audit]) -> None:
    names = {tenant.tenant_id: tenant.company_name for tenant in result.tenants}
    width = max((len(names.get(audit.tenant_id, "")) for audit in audits), default=0)
    console.print()
    console.print(f"[bold]Completed audits, newest first[/] [dim]·[/] {escape(result.module.module_name)} "
                  f"[dim]·[/] {result.year} [dim]·[/] {escape(result.status)} [dim]·[/] "
                  f"{plural(len(audits), 'row')} of {len(result.audits)}")
    console.print()
    for number, audit in enumerate(audits, 1):
        console.print(
            f"  [dim]{number:>4}[/]  {str(audit.audit_date or NOT_SET):<10}  "
            f"{escape(names.get(audit.tenant_id, audit.tenant_id)):<{width}}  "
            f"[dim]completed {_time(audit.end_time)} · job {audit.job_id or NOT_SET} · id {audit.id}[/]",
            highlight=False,
        )
    if audits:
        console.print()


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit code 0 on success (even with no matching rows), 1 on failure."""
    parser = argparse.ArgumentParser(
        prog="python -m app.server.mysql.completed_audits_latest",
        description="Completed data ingestion audits of the active tenants, newest first (read-only).",
    )
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR, help=f"audit_date year (default: {DEFAULT_YEAR})")
    parser.add_argument("--status", default=DEFAULT_STATUS, help=f"audit status (default: {DEFAULT_STATUS})")
    parser.add_argument("--module", default=DEFAULT_MODULE, help=f"ingestion module name (default: {DEFAULT_MODULE})")
    parser.add_argument("-n", "--limit", type=_positive, metavar="COUNT", help="only the COUNT most recent rows")
    parser.add_argument("-j", "--json", action="store_true", help="print the ordered rows as JSON")
    args = parser.parse_args(argv)

    try:
        result = fetch_audits(args.year, args.status, args.module)
    except AuthError as exc:
        status(False, f"Could not read data ingestion audits: {exc}")
        hint(exc.hints)
        return 1

    ordered = newest_first(result.audits)
    audits = ordered[:args.limit] if args.limit else ordered
    if args.json:
        print(json.dumps([audit.to_dict() for audit in audits]))
    else:
        print_latest(result, audits)
    return 0


if __name__ == "__main__":
    sys.exit(main())
