"""Utilization reports shared by every provider: datapoint availability per series, cumulative
summaries per configuration, and their plain-line terminal output.

Provider modules (app/cloud/aws/ec2, app/cloud/azure/vm) fetch the hourly datapoints; this
module stores them in the service's metric tables (app/db/metrics.py), rebuilds cumulative
summaries and prints what was found.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pymysql.connections import Connection
from rich.markup import escape

from app.auth.common import console
from app.cloud.cumulative import CumulativeSummary, shape_line, stats_line, summarize
from app.cloud.display import field, format_time, plural, section
from app.cloud.timeseries import (
    Coverage,
    FetchRequest,
    FetchResult,
    MetricDefinition,
    MetricSeries,
    SeriesKey,
    hours,
    missing_hours,
)
from app.db import resources
from app.db.metrics import MetricTables, MetricTarget


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
        """Hours checked at the provider for which it had no datapoint."""
        return max(self.expected - self.available - self.unchecked_hours, 0)

    @property
    def summary(self) -> CumulativeSummary:
        """Statistics over the datapoints of this selection."""
        return summarize(self.series.datapoints)


@dataclass(frozen=True)
class CumulativeResult:
    """The cumulative summary of one series for the instance's current configuration."""

    instance_id: str
    name: str
    metric: MetricDefinition
    version: str
    effective_since: datetime | None
    summary: CumulativeSummary


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


# ---------------------------------------------------------------- output


def _percent(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "—"


def _print_statistics(label: str, summary: CumulativeSummary, missing_hint: str, extra: str = "") -> None:
    """Print one metric's statistics on two aligned lines, or why there is no data."""
    if summary.datapoint_count == 0:
        field(label, f"{missing_hint}{extra}", "yellow", indent=4)
        return
    field(label, stats_line(summary), indent=4)
    field("", f"{shape_line(summary)}{extra}", "dim", indent=4)


def instance_heading(name: str, instance_id: str, detail: str = "") -> None:
    shown = escape(name) if name else "[dim](no name)[/]"
    console.print(f"  [bold]{shown}[/] [dim]·[/] {escape(instance_id)}" + (f" [dim]· {escape(detail)}[/]" if detail else ""))


def _calls_line(report: UtilizationReport) -> str:
    if report.offline:
        return "offline · not called"
    if not report.api_calls:
        return "no calls · nothing missing"
    calls = f"{report.api_calls} call{'s' if report.api_calls != 1 else ''}"
    return f"{calls} · {report.queries} metric quer{'ies' if report.queries != 1 else 'y'}"


def print_report(
    report: UtilizationReport,
    metrics: Sequence[MetricDefinition],
    monitor: str,
    hourly: bool = False,
    statistics: bool = True,
) -> None:
    """Print datapoint availability, then each instance's metric statistics for the report's
    hours (unless `statistics` is False), optionally every hourly value. `monitor` names the
    provider's metric service, e.g. 'CloudWatch' or 'Azure Monitor'."""
    span = len(hours(report.start, report.end))
    section(f"Datapoints · {format_time(report.start)} → {format_time(report.end)} · {plural(span, 'hour')}", indent=0)
    if not report.series:
        console.print("  [dim]No instances to measure.[/]")
        console.print()
        return

    expected, available = report.total("expected"), report.total("available")
    field(
        "Expected",
        f"{expected} ({plural(report.instances, 'instance')} × {plural(len(metrics), 'metric')} "
        f"× {plural(span, 'hour')})",
    )
    field("Available", f"{available} ({_percent(available, expected)})")
    field("Already stored", f"{report.total('stored_before')} · not requested again")
    field("Fetched now", f"{report.total('fetched_now')} new datapoints")
    field("No data (checked)", f"{plural(report.total('no_data'), 'hour')} · {monitor} had nothing for them")
    if report.total("unchecked_hours"):
        field("Not checked", f"{plural(report.total('unchecked_hours'), 'hour')} · run without --offline to fetch")
    field(monitor, _calls_line(report))
    console.print()

    grouped: dict[str, list[SeriesReport]] = {}
    for item in report.series:
        grouped.setdefault(item.series.instance_id, []).append(item)

    if statistics:
        section(f"Utilization · {format_time(report.start)} → {format_time(report.end)}", indent=0)
        for instance_id, items in sorted(grouped.items(), key=lambda pair: (pair[1][0].name.lower(), pair[0])):
            instance_heading(items[0].name, instance_id)
            for item in items:
                new = f" · +{item.fetched_now} new" if item.fetched_now else ""
                _print_statistics(item.series.metric.label, item.summary, item.series.metric.missing_hint, new)
        console.print()

    if hourly:
        _print_hourly(grouped, metrics)


def print_cumulative(results: list[CumulativeResult]) -> None:
    """Print the cumulative summary of every series for the current configuration."""
    section("Cumulative utilization · current configuration", indent=0)
    if not results:
        console.print("  [dim]No instances to measure.[/]")
        console.print()
        return

    grouped: dict[str, list[CumulativeResult]] = {}
    for result in results:
        grouped.setdefault(result.instance_id, []).append(result)
    for instance_id, items in sorted(grouped.items(), key=lambda pair: (pair[1][0].name.lower(), pair[0])):
        first = items[0]
        since = f"since {format_time(first.effective_since)}" if first.effective_since else "since tracking began"
        instance_heading(first.name, instance_id, f"config {first.version[:8]} · {since}")
        for item in items:
            missing = (
                f"no datapoints yet for this configuration (took effect {format_time(item.effective_since)})"
                if item.effective_since else item.metric.missing_hint
            )
            _print_statistics(item.metric.label, item.summary, missing)
    console.print()


def _print_hourly(grouped: Mapping[str, list[SeriesReport]], metrics: Sequence[MetricDefinition]) -> None:
    for instance_id, items in sorted(grouped.items()):
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
                for metric in metrics
            )
            console.print(f"  [dim]{format_time(hour)}[/]  {readings}")
        console.print()


# ---------------------------------------------------------------- collect and store


def save_fetched(
    tables: MetricTables,
    connection: Connection,
    run_id: int,
    fetched_at: datetime,
    targets: Mapping[str, MetricTarget],
    requests: Sequence[FetchRequest],
    result: FetchResult,
) -> None:
    """Store the fetched datapoints and record every requested range as checked."""
    for request in requests:
        target = targets.get(request.instance_id)
        if target is None:
            continue
        points = result.points.get(request.key, ())
        tables.save_datapoints(connection, target, request.instance_id, request.metric, run_id, fetched_at, points)
        tables.record_fetch(connection, target, request, run_id, len(points), fetched_at)


def build_report(
    tables: MetricTables,
    connection: Connection,
    instance_scope: resources.Scope,
    start: datetime,
    end: datetime,
    targets: Mapping[str, MetricTarget],
    coverage: Mapping[SeriesKey, Coverage],
    requests: Sequence[FetchRequest],
    result: FetchResult,
    offline: bool = False,
) -> UtilizationReport:
    """Read every stored datapoint in [start, end) and compare it with what was known before."""
    requested = {request.key: request for request in requests}
    reports = []
    for series in tables.load_series(connection, instance_scope, start, end, targets):
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
    tables: MetricTables,
    connection: Connection,
    run_id: int,
    targets: Mapping[str, MetricTarget],
    report: UtilizationReport,
) -> None:
    """Store the per-series summary of a run in the service's metric collections table."""
    for item in report.series:
        tables.record_collection(
            connection, targets[item.series.instance_id], item.series, run_id, item.requested_hours, item.fetched_now
        )


def update_cumulative(
    tables: MetricTables,
    connection: Connection,
    instance_scope: resources.Scope,
    run_id: int,
    updated_at: datetime,
    targets: Mapping[str, MetricTarget],
    versions: Mapping[str, str],
) -> list[CumulativeResult]:
    """Rebuild and store the cumulative summary of every series from the raw datapoints.

    Evidence is every stored hour since the instance's current configuration took effect
    (its last spec change), or every stored hour if no spec change was ever detected.
    `versions` maps each instance to its configuration version.
    """
    since = tables.configuration_effective_since(connection, instance_scope, targets)
    points = tables.load_points_since(connection, instance_scope, since)
    results = []
    for instance_id in sorted(targets):
        target, version = targets[instance_id], versions[instance_id]
        for metric in tables.metrics:
            summary = summarize(points.get((instance_id, metric.key), ()))
            tables.save_cumulative(
                connection, target, instance_id, metric, version, since[instance_id], summary, run_id, updated_at
            )
            results.append(CumulativeResult(instance_id, target.name, metric, version, since[instance_id], summary))
    return results
