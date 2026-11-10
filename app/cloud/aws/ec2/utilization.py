"""EC2 utilization: fetch only the missing hourly CPU/memory datapoints and report availability.

Used by every analysis run and by the quick `metrics` command:
  1. load which hours are stored and which were already checked in CloudWatch
  2. request only the missing hours (nothing missing means no CloudWatch call at all)
  3. store the new datapoints and record the requested ranges as checked
  4. report, per instance and metric: expected, available, already stored, fetched now,
     no data in CloudWatch, plus average and peak hourly average
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from pymysql.connections import Connection
from rich.markup import escape

from app import db
from app.auth import aws_auth
from app.auth.common import console
from app.cloud.aws.ec2 import store
from app.cloud.aws.ec2.metrics import (
    METRICS,
    PERIOD,
    SETTLE_DELAY,
    Coverage,
    FetchRequest,
    FetchResult,
    MetricSeries,
    SeriesKey,
    fetch,
    hours,
    missing_hours,
    plan_requests,
    settled_end,
)
from app.cloud.aws.ec2.windows import AnalysisWindow
from app.cloud.display import field, format_time, plural, section
from app.db import resources

ADHOC_PURPOSE = "adhoc"


@dataclass(frozen=True)
class Selection:
    """The hours a quick analysis covers."""

    key: str
    label: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class SeriesReport:
    """Availability and utilization of one metric for one instance."""

    name: str
    series: MetricSeries
    stored_before: int
    requested_hours: int
    unchecked_hours: int

    @property
    def expected(self) -> int:
        return self.series.expected_points

    @property
    def available(self) -> int:
        return self.series.collected_points

    @property
    def fetched_now(self) -> int:
        return self.available - self.stored_before

    @property
    def no_data(self) -> int:
        """Hours checked in CloudWatch for which it had no datapoint."""
        return max(self.expected - self.available - self.unchecked_hours, 0)


@dataclass(frozen=True)
class UtilizationReport:
    """What a run or quick analysis found for every instance and metric in its hours."""

    start: datetime
    end: datetime
    series: list[SeriesReport]
    api_calls: int
    queries: int
    offline: bool = False

    def total(self, attribute: str) -> int:
        return sum(getattr(item, attribute) for item in self.series)

    @property
    def instances(self) -> int:
        return len({item.series.instance_id for item in self.series})


# ---------------------------------------------------------------- selections


def window_selection(window: AnalysisWindow, now: datetime | None = None) -> Selection:
    start, end = window.bounds(now)
    return Selection(window.key, f"{window.key} · {window.purpose} · {window.description}", start, end)


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
    for request in requests:
        target = targets.get(request.instance_id)
        if target is None:
            continue
        points = result.points.get(request.key, ())
        store.save_datapoints(connection, target, request.instance_id, request.metric, run_id, fetched_at, points)
        store.record_fetch(connection, target, request, run_id, len(points), fetched_at)


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
    requested = {request.key: request for request in requests}
    reports = []
    for series in store.load_series(connection, instance_scope, start, end, targets):
        key = (series.instance_id, series.metric.key)
        known = coverage.get(key, Coverage())
        stored_before = sum(1 for hour in known.stored if start <= hour < end)
        request = requested.get(key)
        reports.append(SeriesReport(
            name=targets[series.instance_id].name,
            series=series,
            stored_before=stored_before,
            requested_hours=request.hours if request else 0,
            unchecked_hours=0 if request else len(missing_hours(start, end, known)),
        ))
    return UtilizationReport(start, end, reports, result.api_calls, result.queries, offline)


def record_report(
    connection: Connection,
    run_id: int,
    targets: Mapping[str, store.MetricTarget],
    report: UtilizationReport,
) -> None:
    """Store the per-series summary of a run in ec2_metric_collections."""
    for item in report.series:
        store.record_collection(
            connection, targets[item.series.instance_id], item.series, run_id, item.requested_hours, item.fetched_now
        )


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


def _percent(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "—"


def summary_line(item: SeriesReport) -> str:
    """Return e.g. 'avg 12.4% · peak 45.1% · 223/223 hours · +2 new'."""
    hours_text = f"{item.available}/{plural(item.expected, 'hour')}"
    if item.available < item.expected:
        hours_text += f" ({_percent(item.available, item.expected)})"
    new = f" · +{item.fetched_now} new" if item.fetched_now else ""
    if item.series.average is None:
        return f"{item.series.metric.missing_hint} · {hours_text}"
    return f"avg {item.series.average:.1f}% · peak {item.series.peak:.1f}% · {hours_text}{new}"


def _cloudwatch_line(report: UtilizationReport) -> str:
    if report.offline:
        return "offline · not called"
    if not report.api_calls:
        return "no calls · nothing missing"
    calls = f"{report.api_calls} call{'s' if report.api_calls != 1 else ''}"
    return f"{calls} · {report.queries} metric quer{'ies' if report.queries != 1 else 'y'}"


def print_report(report: UtilizationReport, hourly: bool = False) -> None:
    """Print datapoint availability, then each instance's CPU and memory, optionally every hour."""
    span = len(hours(report.start, report.end))
    section(f"Datapoints · {format_time(report.start)} → {format_time(report.end)} · {plural(span, 'hour')}", indent=0)
    if not report.series:
        console.print("  [dim]No instances to measure.[/]")
        console.print()
        return

    expected, available = report.total("expected"), report.total("available")
    field(
        "Expected",
        f"{expected} ({plural(report.instances, 'instance')} × {plural(len(METRICS), 'metric')} "
        f"× {plural(span, 'hour')})",
    )
    field("Available", f"{available} ({_percent(available, expected)})")
    field("Already stored", f"{report.total('stored_before')} · not requested again")
    field("Fetched now", f"{report.total('fetched_now')} new datapoints")
    field("No data (checked)", f"{plural(report.total('no_data'), 'hour')} · CloudWatch had nothing for them")
    if report.total("unchecked_hours"):
        field("Not checked", f"{plural(report.total('unchecked_hours'), 'hour')} · run without --offline to fetch")
    field("CloudWatch", _cloudwatch_line(report))
    console.print()

    section("Utilization · hourly averages", indent=0)
    by_instance: dict[str, list[SeriesReport]] = {}
    for item in report.series:
        by_instance.setdefault(item.series.instance_id, []).append(item)
    for instance_id, items in sorted(by_instance.items(), key=lambda pair: (pair[1][0].name.lower(), pair[0])):
        name = escape(items[0].name) if items[0].name else "[dim](no name)[/]"
        console.print(f"  [bold]{name}[/] [dim]·[/] {instance_id}")
        for item in items:
            field(item.series.metric.label, summary_line(item), "" if item.available else "yellow", indent=4)
    console.print()

    if hourly:
        _print_hourly(by_instance)


def _print_hourly(by_instance: Mapping[str, list[SeriesReport]]) -> None:
    for instance_id, items in sorted(by_instance.items()):
        section(f"Hourly averages · {items[0].name or '(no name)'} · {instance_id}", indent=0)
        values = {
            (item.series.metric.key, point.period_start): point.average
            for item in items
            for point in item.series.datapoints
        }
        span = hours(items[0].series.window_start, items[0].series.window_end)
        for hour in span:
            readings = " [dim]·[/] ".join(
                f"{metric.label} {values[(metric.key, hour)]:.1f}%" if (metric.key, hour) in values
                else f"{metric.label} [dim]—[/]"
                for metric in METRICS
            )
            console.print(f"  [dim]{format_time(hour)}[/]  {readings}")
        console.print()