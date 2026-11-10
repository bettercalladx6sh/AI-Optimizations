"""EC2 utilization: fetch only the missing hourly CPU/memory datapoints and report availability.

Used by every analysis run and by the quick `metrics` command:
  1. load which hours are stored and which were already checked in CloudWatch
  2. request only the missing hours (nothing missing means no CloudWatch call at all)
  3. store the new datapoints and record the requested ranges as checked
  4. report, per instance and metric: expected, available, already stored, fetched now and
     no data in CloudWatch, plus statistics (average, percentiles, trend, volatility, ...)

Analysis runs also rebuild the cumulative summary of every series: statistics over every
stored hour since the instance's current configuration took effect.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from pymysql.connections import Connection

from app import db
from app.auth import aws_auth
from app.cloud.aws.ec2 import store
from app.cloud.aws.ec2.changes import InstanceSnapshot, configuration_version
from app.cloud.aws.ec2.metrics import (
    METRICS,
    PERIOD,
    SETTLE_DELAY,
    Coverage,
    FetchRequest,
    FetchResult,
    SeriesKey,
    fetch,
    plan_requests,
    settled_end,
)
from app.cloud import utilization as shared
from app.cloud.display import format_time
from app.cloud.utilization import (
    CumulativeResult,
    SeriesReport,
    UtilizationReport,
    print_cumulative,
)
from app.cloud.utilization import print_report as shared_print_report
from app.cloud.windows import AnalysisWindow
from app.db import resources

# CumulativeResult, SeriesReport, UtilizationReport and print_cumulative are shared
# (app/cloud/utilization.py) and re-exported here for the EC2 modules.
__all__ = [
    "ADHOC_PURPOSE", "CumulativeResult", "SeriesReport", "Selection", "UtilizationReport", "analyze",
    "build_report", "day_selection", "hour_selection", "print_cumulative", "print_report", "record_report",
    "save_fetched", "update_cumulative", "window_selection",
]

ADHOC_PURPOSE = "adhoc"


@dataclass(frozen=True)
class Selection:
    """The hours a quick analysis covers."""

    key: str
    label: str
    start: datetime
    end: datetime


# ---------------------------------------------------------------- selections


def window_selection(window: AnalysisWindow, now: datetime | None = None) -> Selection:
    start, end = window.bounds(now)
    return Selection(window.key, window.label(start, end), start, end)


def day_selection(text: str, now: datetime | None = None) -> Selection:
    """'today', 'yesterday' or YYYY-MM-DD: that UTC day, up to the last settled hour."""
    now = now or datetime.now(timezone.utc)
    words = {"today": now.date(), "yesterday": now.date() - timedelta(days=1)}
    try:
        day = words.get(text.lower()) or date.fromisoformat(text)
    except ValueError:
        raise ValueError("use YYYY-MM-DD, 'today' or 'yesterday'") from None
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = min(start + timedelta(days=1), settled_end(now))
    if end <= start:
        raise ValueError(f"no settled hour yet on {day} (hours settle {_minutes(SETTLE_DELAY)} after they end)")
    return Selection("day", f"day {day}", start, end)


def hour_selection(text: str, now: datetime | None = None) -> Selection:
    """'YYYY-MM-DD HH', 'YYYY-MM-DDTHH' or 'HH' (today, UTC): that single hour."""
    now = now or datetime.now(timezone.utc)
    value = text.strip().replace("T", " ")
    try:
        if len(value) <= 2:
            start = datetime(now.year, now.month, now.day, int(value), tzinfo=timezone.utc)
        else:
            start = datetime.strptime(value[:13], "%Y-%m-%d %H").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("use 'YYYY-MM-DD HH' (UTC) or 'HH' for today") from None
    end = start + PERIOD
    if end > settled_end(now):
        raise ValueError(
            f"{format_time(start)} is not settled yet (hours settle {_minutes(SETTLE_DELAY)} after they end)"
        )
    return Selection("hour", f"hour {format_time(start)}", start, end)


def _minutes(delta: timedelta) -> str:
    return f"{int(delta.total_seconds() // 60)} min"


# ---------------------------------------------------------------- collect and store


def save_fetched(
    connection: Connection,
    run_id: int,
    fetched_at: datetime,
    targets: Mapping[str, store.MetricTarget],
    requests: list[FetchRequest],
    result: FetchResult,
) -> None:
    """Store the fetched datapoints and record every requested range as checked."""
    shared.save_fetched(store.TABLES, connection, run_id, fetched_at, targets, requests, result)


def build_report(
    connection: Connection,
    instance_scope: resources.Scope,
    start: datetime,
    end: datetime,
    targets: Mapping[str, store.MetricTarget],
    coverage: Mapping[SeriesKey, Coverage],
    requests: list[FetchRequest],
    result: FetchResult,
    offline: bool = False,
) -> UtilizationReport:
    """Read every stored datapoint in [start, end) and compare it with what was known before."""
    return shared.build_report(store.TABLES, connection, instance_scope, start, end, targets, coverage,
                               requests, result, offline)


def record_report(
    connection: Connection,
    run_id: int,
    targets: Mapping[str, store.MetricTarget],
    report: UtilizationReport,
) -> None:
    """Store the per-series summary of a run in ec2_metric_collections."""
    shared.record_report(store.TABLES, connection, run_id, targets, report)


def update_cumulative(
    connection: Connection,
    instance_scope: resources.Scope,
    run_id: int,
    updated_at: datetime,
    targets: Mapping[str, store.MetricTarget],
    snapshots: Mapping[str, InstanceSnapshot],
) -> list[CumulativeResult]:
    """Rebuild and store the cumulative summary of every series from the raw datapoints."""
    return shared.update_cumulative(store.TABLES, connection, instance_scope, run_id, updated_at, targets,
                                    {iid: configuration_version(snapshot) for iid, snapshot in snapshots.items()})


def analyze(
    selection: Selection,
    region: str,
    resource: str | None = None,
    offline: bool = False,
) -> UtilizationReport:
    """Quick analysis of stored instances: fill missing hours (unless offline) and report.

    Covers instances stored by an earlier analysis run, optionally one by ID or Name tag.
    A quick analysis that fetches data is recorded as an 'adhoc' run.
    """
    now = datetime.now(timezone.utc)
    account_id = aws_auth.get_identity().account_id

    with db.session() as connection:
        account_ref = resources.upsert_account(connection, store.PROVIDER, account_id, now)
        instance_scope = store.scope(account_ref, region)
        targets = store.metric_targets(connection, instance_scope, resource)
        coverage = store.load_coverage(connection, instance_scope, selection.start, selection.end, targets)

    requests = [] if offline else plan_requests(targets, selection.start, selection.end, coverage)
    result = fetch(requests, region)

    with db.session() as connection:
        run_id = None
        if requests:
            run_id = resources.start_run(
                connection, instance_scope, selection.key,
                int((selection.end - selection.start).total_seconds()), ADHOC_PURPOSE, now,
            )
            save_fetched(connection, run_id, now, targets, requests, result)
        report = build_report(
            connection, instance_scope, selection.start, selection.end, targets, coverage, requests, result, offline
        )
        if run_id:
            record_report(connection, run_id, targets, report)
            resources.finish_run(connection, run_id, datetime.now(timezone.utc), len(targets), 0, False)
    return report


# ---------------------------------------------------------------- output


def print_report(report: UtilizationReport, hourly: bool = False, statistics: bool = True) -> None:
    """Print datapoint availability and EC2 utilization (see app.cloud.utilization.print_report)."""
    shared_print_report(report, METRICS, "CloudWatch", hourly, statistics)
