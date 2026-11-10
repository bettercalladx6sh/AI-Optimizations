"""Metric storage shared by every service: hourly averages, checked hour ranges, per-run
collections and cumulative summaries, in one set of tables per service.

    <prefix>_metric_datapoints   one row per instance, metric and hour: the hourly average
    <prefix>_metric_fetches      every hour range requested from the provider (never requested again)
    <prefix>_metric_collections  what each run found per instance and metric
    <prefix>_metric_cumulative   statistics per instance, metric and configuration version

The prefix is 'ec2' for EC2 instances and 'azure_vm' for Azure VMs (see app/db/migrations).
Resources, snapshots and changes are stored by the generic app/db/resources.py.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pymysql.connections import Connection

from app.cloud.cumulative import CumulativeSummary
from app.cloud.timeseries import (
    PERIOD,
    STATISTIC,
    Coverage,
    Datapoint,
    FetchRequest,
    MetricDefinition,
    MetricSeries,
    SeriesKey,
)
from app.db import resources
from app.db.resources import from_db, to_db

PREFIXES = ("ec2", "azure_vm")


@dataclass(frozen=True)
class MetricTarget:
    """A stored instance whose metrics can be saved: its resource row and current name."""

    ref: int
    name: str


def _instance_filter(instance_ids: Iterable[str]) -> tuple[str, list[str]]:
    ids = sorted(set(instance_ids))
    return ", ".join(["%s"] * len(ids)), ids


def _scope_params(instance_scope: resources.Scope) -> list[Any]:
    return [instance_scope.account_ref, instance_scope.region, instance_scope.service, instance_scope.resource_type]


SCOPE_SQL = "r.account_ref = %s AND r.region = %s AND r.service = %s AND r.resource_type = %s"


class MetricTables:
    """The metric tables of one service, e.g. MetricTables('ec2', METRICS, SPEC_FIELDS)."""

    def __init__(self, prefix: str, metrics: Sequence[MetricDefinition], spec_fields: Sequence[str]) -> None:
        if prefix not in PREFIXES:
            raise ValueError(f"unknown metric table prefix {prefix!r}")
        self.prefix = prefix
        self.metrics = tuple(metrics)
        self.spec_fields = frozenset(spec_fields)
        self.datapoints = f"{prefix}_metric_datapoints"
        self.fetches = f"{prefix}_metric_fetches"
        self.collections = f"{prefix}_metric_collections"
        self.cumulative = f"{prefix}_metric_cumulative"

    def with_metrics(self, metrics: Sequence[MetricDefinition]) -> MetricTables:
        """The same tables for another metric set (e.g. Azure memory from Log Analytics)."""
        return MetricTables(self.prefix, metrics, tuple(self.spec_fields))

    def metric_targets(
        self,
        connection: Connection,
        instance_scope: resources.Scope,
        resource_filter: str | None = None,
    ) -> dict[str, MetricTarget]:
        """Stored instances that still exist and are not terminated, optionally one by ID or name."""
        query = (
            "SELECT resource_id, id, COALESCE(name, '') FROM resources "
            "WHERE account_ref = %s AND region = %s AND service = %s AND resource_type = %s "
            "AND removed_at IS NULL AND COALESCE(state, '') <> 'terminated'"
        )
        params = _scope_params(instance_scope)
        if resource_filter:
            query += " AND (resource_id = %s OR name = %s)"
            params += [resource_filter, resource_filter]
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            return {instance_id: MetricTarget(ref, name) for instance_id, ref, name in cursor.fetchall()}

    def load_coverage(
        self,
        connection: Connection,
        instance_scope: resources.Scope,
        start: datetime,
        end: datetime,
        instance_ids: Iterable[str],
    ) -> dict[SeriesKey, Coverage]:
        """For each instance and metric: hours stored in the window and ranges already checked."""
        placeholders, ids = _instance_filter(instance_ids)
        if not ids:
            return {}
        scope_params = [*_scope_params(instance_scope), *ids]
        scope_sql = f"{SCOPE_SQL} AND r.resource_id IN ({placeholders})"
        stored: dict[SeriesKey, set[datetime]] = defaultdict(set)
        checked: dict[SeriesKey, list[tuple[datetime, datetime]]] = defaultdict(list)

        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT r.resource_id, d.metric, d.period_start FROM {self.datapoints} d "
                f"JOIN resources r ON r.id = d.resource_ref WHERE {scope_sql} "
                "AND d.period_start >= %s AND d.period_start < %s",
                [*scope_params, to_db(start), to_db(end)],
            )
            for instance_id, metric_key, period_start in cursor.fetchall():
                stored[(instance_id, metric_key)].add(from_db(period_start))

            cursor.execute(
                f"SELECT r.resource_id, f.metric, f.range_start, f.range_end FROM {self.fetches} f "
                f"JOIN resources r ON r.id = f.resource_ref WHERE {scope_sql} "
                "AND f.range_end > %s AND f.range_start < %s",
                [*scope_params, to_db(start), to_db(end)],
            )
            for instance_id, metric_key, range_start, range_end in cursor.fetchall():
                checked[(instance_id, metric_key)].append((from_db(range_start), from_db(range_end)))

        return {
            key: Coverage(frozenset(stored.get(key, ())), tuple(checked.get(key, ())))
            for key in stored.keys() | checked.keys()
        }

    def save_datapoints(
        self,
        connection: Connection,
        target: MetricTarget,
        instance_id: str,
        metric: MetricDefinition,
        run_id: int,
        fetched_at: datetime,
        points: Iterable[Datapoint],
    ) -> None:
        """Store hourly averages: one row per instance, metric and hour, never duplicated."""
        rows = [
            (target.ref, instance_id, target.name or None, metric.key, to_db(point.period_start),
             int(PERIOD.total_seconds()), point.average, metric.unit, run_id, to_db(fetched_at))
            for point in points
        ]
        if not rows:
            return
        with connection.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO {self.datapoints} (resource_ref, instance_id, instance_name, metric, period_start, "
                "period_seconds, average, unit, run_id, collected_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) AS new "
                "ON DUPLICATE KEY UPDATE instance_name = new.instance_name, average = new.average, "
                "run_id = new.run_id, collected_at = new.collected_at",
                rows,
            )

    def record_fetch(
        self,
        connection: Connection,
        target: MetricTarget,
        request: FetchRequest,
        run_id: int,
        returned_points: int,
        fetched_at: datetime,
    ) -> None:
        """Record an hour range as checked at the provider, so it is never requested again."""
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self.fetches} (run_id, resource_ref, instance_id, instance_name, metric, "
                "range_start, range_end, requested_hours, returned_points, fetched_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (run_id, target.ref, request.instance_id, target.name or None, request.metric.key,
                 to_db(request.start), to_db(request.end), request.hours, returned_points, to_db(fetched_at)),
            )

    def record_collection(
        self,
        connection: Connection,
        target: MetricTarget,
        series: MetricSeries,
        run_id: int,
        requested_points: int,
        fetched_points: int,
    ) -> None:
        """Record, for one run, what was available, requested and fetched for one series."""
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self.collections} (run_id, resource_ref, instance_id, instance_name, metric, "
                "namespace, metric_name, statistic, period_seconds, window_start, window_end, expected_points, "
                "requested_points, fetched_points, collected_points) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (run_id, target.ref, series.instance_id, target.name or None, series.metric.key,
                 series.metric.namespace, series.metric.metric_name, STATISTIC, int(PERIOD.total_seconds()),
                 to_db(series.window_start), to_db(series.window_end), series.expected_points, requested_points,
                 fetched_points, series.collected_points),
            )

    def load_series(
        self,
        connection: Connection,
        instance_scope: resources.Scope,
        start: datetime,
        end: datetime,
        instance_ids: Iterable[str],
    ) -> list[MetricSeries]:
        """Every stored hourly average in the window, one series per instance and metric (even if empty)."""
        placeholders, ids = _instance_filter(instance_ids)
        if not ids:
            return []
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT r.resource_id, d.metric, d.period_start, d.average "
                f"FROM {self.datapoints} d JOIN resources r ON r.id = d.resource_ref "
                f"WHERE {SCOPE_SQL} AND r.resource_id IN ({placeholders}) "
                "AND d.period_start >= %s AND d.period_start < %s ORDER BY d.period_start",
                [*_scope_params(instance_scope), *ids, to_db(start), to_db(end)],
            )
            rows = cursor.fetchall()

        points: dict[SeriesKey, list[Datapoint]] = defaultdict(list)
        for instance_id, metric_key, period_start, average in rows:
            points[(instance_id, metric_key)].append(Datapoint(from_db(period_start), average))

        return [
            MetricSeries(instance_id, metric, start, end, tuple(points[(instance_id, metric.key)]))
            for instance_id in ids
            for metric in self.metrics
        ]

    def configuration_effective_since(
        self,
        connection: Connection,
        instance_scope: resources.Scope,
        instance_ids: Iterable[str],
    ) -> dict[str, datetime | None]:
        """When each instance's current configuration took effect: its last detected spec change.

        None means no spec change was ever detected, so every stored hour belongs to it.
        """
        placeholders, ids = _instance_filter(instance_ids)
        since: dict[str, datetime | None] = dict.fromkeys(ids)
        if not ids:
            return since
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT r.resource_id, c.detected_at, c.field_changes FROM resource_changes c "
                "JOIN resources r ON r.id = c.resource_ref "
                f"WHERE {SCOPE_SQL} AND r.resource_id IN ({placeholders}) AND c.change_type = 'modified' "
                "ORDER BY c.detected_at",
                [*_scope_params(instance_scope), *ids],
            )
            for instance_id, detected_at, field_changes in cursor.fetchall():
                if any(item["field"] in self.spec_fields for item in json.loads(field_changes)):
                    since[instance_id] = from_db(detected_at)
        return since

    def load_points_since(
        self,
        connection: Connection,
        instance_scope: resources.Scope,
        since: dict[str, datetime | None],
    ) -> dict[SeriesKey, list[Datapoint]]:
        """Every stored hourly average of each instance from its `since` time (or all of them)."""
        placeholders, ids = _instance_filter(since)
        if not ids:
            return {}
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT r.resource_id, d.metric, d.period_start, d.average "
                f"FROM {self.datapoints} d JOIN resources r ON r.id = d.resource_ref "
                f"WHERE {SCOPE_SQL} AND r.resource_id IN ({placeholders}) ORDER BY d.period_start",
                [*_scope_params(instance_scope), *ids],
            )
            rows = cursor.fetchall()

        points: dict[SeriesKey, list[Datapoint]] = defaultdict(list)
        for instance_id, metric_key, period_start, average in rows:
            moment = from_db(period_start)
            start = since.get(instance_id)
            if start is None or moment >= start:
                points[(instance_id, metric_key)].append(Datapoint(moment, average))
        return points

    def save_cumulative(
        self,
        connection: Connection,
        target: MetricTarget,
        instance_id: str,
        metric: MetricDefinition,
        version: str,
        effective_since: datetime | None,
        summary: CumulativeSummary,
        run_id: int,
        updated_at: datetime,
    ) -> None:
        """Insert or rebuild the cumulative summary of one series for its configuration version."""
        columns = {
            "resource_ref": target.ref,
            "instance_id": instance_id,
            "instance_name": target.name or None,
            "metric": metric.key,
            "configuration_version": version,
            "effective_since": to_db(effective_since),
            "evidence_start": to_db(summary.evidence_start),
            "evidence_end": to_db(summary.evidence_end),
            "datapoint_count": summary.datapoint_count,
            "value_sum": summary.value_sum,
            "average": summary.average,
            "minimum": summary.minimum,
            "maximum": summary.maximum,
            "p50": summary.p50,
            "p95": summary.p95,
            "p99": summary.p99,
            "standard_deviation": summary.standard_deviation,
            "trend": summary.trend,
            "volatility": summary.volatility,
            "coverage_percent": summary.coverage_percent,
            "gap_count": summary.gap_count,
            "run_id": run_id,
            "updated_at": to_db(updated_at),
        }
        keys = ("resource_ref", "metric", "configuration_version")
        names = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))
        updates = ", ".join(f"{name} = new.{name}" for name in columns if name not in keys)
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {self.cumulative} ({names}) VALUES ({placeholders}) AS new "
                f"ON DUPLICATE KEY UPDATE {updates}",
                tuple(columns.values()),
            )
