"""EC2 storage: write discovered instances into the analysis database.

Each instance is stored as a generic resource (resources, resource_snapshots,
resource_tags, resource_changes via app.db.resources) plus EC2-specific typed rows
(ec2_instances, ec2_volumes) for querying its current configuration, and its hourly
CPU and memory averages (ec2_metric_datapoints), the hour ranges already checked in
CloudWatch (ec2_metric_fetches), a per-run summary (ec2_metric_collections) and the
cumulative summary per configuration version (ec2_metric_cumulative).
"""

from __future__ import annotations
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from pymysql.connections import Connection

from app.cloud.aws.ec2.changes import SPEC_FIELDS, FieldChange, InstanceChange, InstanceSnapshot
from app.cloud.aws.ec2.cumulative import CumulativeSummary
from app.cloud.aws.ec2.configuration import InstanceConfiguration
from app.cloud.aws.ec2.metrics import (
    METRICS,
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
from app.db.resources import ChangeRecord, StoredResource, from_db, to_db, to_json

PROVIDER = "aws"
SERVICE = "ec2"
RESOURCE_TYPE = "instance"


def scope(account_ref: int, region: str) -> resources.Scope:
    """The storage scope for EC2 instances in one account and region."""
    return resources.Scope(account_ref, region, SERVICE, RESOURCE_TYPE)


def configuration_document(config: InstanceConfiguration) -> dict[str, Any]:
    """The full configuration as a JSON-ready dict (stored in resource_snapshots)."""
    return asdict(config)


def save_instance(
    connection: Connection,
    instance_scope: resources.Scope,
    run_id: int,
    seen_at: datetime,
    config: InstanceConfiguration,
    snapshot: InstanceSnapshot,
    stored: StoredResource | None,
    changed: bool,
) -> tuple[int, int]:
    """Store one instance seen in this run; return (resource row ID, current snapshot ID)."""
    resource = resources.save_resource(
        connection, instance_scope, snapshot.instance_id, snapshot.name, snapshot.state, run_id, seen_at, changed
    )
    snapshot_id = resources.save_snapshot(
        connection, resource, run_id, seen_at, snapshot.to_dict(), configuration_document(config), stored
    )
    resources.replace_tags(connection, resource, config.tags)
    _save_details(connection, resource, config, snapshot, seen_at)
    return resource, snapshot_id


def _save_details(
    connection: Connection,
    resource: int,
    config: InstanceConfiguration,
    snapshot: InstanceSnapshot,
    updated_at: datetime,
) -> None:
    """Replace the typed ec2_instances and ec2_volumes rows with the current configuration."""
    spec = config.type_spec
    instance = config.instance
    columns = {
        "resource_ref": resource,
        "instance_type": config.instance_type,
        "family": snapshot.family,
        "size": snapshot.size,
        "vcpus": spec.vcpus if spec else None,
        "memory_gib": spec.memory_gib if spec else None,
        "network_performance": spec.network if spec else None,
        "burstable": spec.burstable if spec else None,
        "current_generation": spec.current_generation if spec else None,
        "architecture": config.architecture or None,
        "platform": config.platform or None,
        "lifecycle": config.lifecycle or None,
        "tenancy": config.tenancy or None,
        "availability_zone": config.availability_zone or None,
        "vpc_id": instance.vpc_id or None,
        "subnet_id": config.subnet_id or None,
        "public_ip": instance.public_ip or None,
        "private_ip": instance.private_ip or None,
        "ami_id": config.ami_id or None,
        "key_name": config.key_name or None,
        "iam_profile": config.iam_profile or None,
        "security_groups": to_json(list(config.security_groups)),
        "ebs_optimized": config.ebs_optimized,
        "detailed_monitoring": config.detailed_monitoring,
        "root_device": config.root_device or None,
        "storage_gib": snapshot.storage_gib,
        "launch_time": to_db(config.launch_time),
        "updated_at": to_db(updated_at),
    }
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"{name} = new.{name}" for name in columns if name != "resource_ref")

    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO ec2_instances ({names}) VALUES ({placeholders}) AS new ON DUPLICATE KEY UPDATE {updates}",
            tuple(columns.values()),
        )
        cursor.execute("DELETE FROM ec2_volumes WHERE resource_ref = %s", (resource,))
        volumes = [
            (resource, v.volume_id, v.device, v.size_gib, v.volume_type or None, v.iops,
             v.throughput, v.encrypted, v.delete_on_termination)
            for v in config.volumes
        ]
        if volumes:
            cursor.executemany(
                "INSERT INTO ec2_volumes (resource_ref, volume_id, device, size_gib, volume_type, iops, "
                "throughput_mibps, encrypted, delete_on_termination) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                volumes,
            )


def record_change(
    connection: Connection,
    resource: int,
    run_id: int,
    detected_at: datetime,
    change: InstanceChange,
    before_snapshot_id: int | None,
    after_snapshot_id: int | None,
) -> None:
    """Store one detected instance change."""
    resources.record_change(
        connection,
        resource,
        run_id,
        detected_at,
        change.kind,
        change.significant,
        ({"field": item.field, "before": item.before, "after": item.after} for item in change.fields),
        before_snapshot_id,
        after_snapshot_id,
    )


def to_instance_change(record: ChangeRecord) -> InstanceChange:
    """Rebuild an InstanceChange from a stored change record."""
    return InstanceChange(
        kind=record.change_type,
        before=InstanceSnapshot.from_dict(record.before) if record.before else None,
        after=InstanceSnapshot.from_dict(record.after) if record.after else None,
        fields=tuple(FieldChange(item["field"], item["before"], item["after"]) for item in record.field_changes),
    )

@dataclass(frozen=True)
class MetricTarget:
    """A stored instance whose metrics can be saved: its resource row and current name."""

    ref: int
    name: str


def _instance_filter(instance_ids: Iterable[str]) -> tuple[str, list[str]]:
    ids = sorted(set(instance_ids))
    return ", ".join(["%s"] * len(ids)), ids


def metric_targets(
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
    params: list[Any] = [instance_scope.account_ref, instance_scope.region, instance_scope.service,
                         instance_scope.resource_type]
    if resource_filter:
        query += " AND (resource_id = %s OR name = %s)"
        params += [resource_filter, resource_filter]
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return {instance_id: MetricTarget(ref, name) for instance_id, ref, name in cursor.fetchall()}


def load_coverage(
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
    scope_params = [instance_scope.account_ref, instance_scope.region, instance_scope.service,
                    instance_scope.resource_type, *ids]
    scope_sql = (
        "r.account_ref = %s AND r.region = %s AND r.service = %s AND r.resource_type = %s "
        f"AND r.resource_id IN ({placeholders})"
    )
    stored: dict[SeriesKey, set[datetime]] = defaultdict(set)
    checked: dict[SeriesKey, list[tuple[datetime, datetime]]] = defaultdict(list)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT r.resource_id, d.metric, d.period_start FROM ec2_metric_datapoints d "
            f"JOIN resources r ON r.id = d.resource_ref WHERE {scope_sql} "
            "AND d.period_start >= %s AND d.period_start < %s",
            [*scope_params, to_db(start), to_db(end)],
        )
        for instance_id, metric_key, period_start in cursor.fetchall():
            stored[(instance_id, metric_key)].add(from_db(period_start))

        cursor.execute(
            "SELECT r.resource_id, f.metric, f.range_start, f.range_end FROM ec2_metric_fetches f "
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
            "INSERT INTO ec2_metric_datapoints (resource_ref, instance_id, instance_name, metric, period_start, "
            "period_seconds, average, unit, run_id, collected_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE instance_name = new.instance_name, average = new.average, "
            "run_id = new.run_id, collected_at = new.collected_at",
            rows,
        )


def record_fetch(
    connection: Connection,
    target: MetricTarget,
    request: FetchRequest,
    run_id: int,
    returned_points: int,
    fetched_at: datetime,
) -> None:
    """Record an hour range as checked in CloudWatch, so it is never requested again."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO ec2_metric_fetches (run_id, resource_ref, instance_id, instance_name, metric, "
            "range_start, range_end, requested_hours, returned_points, fetched_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (run_id, target.ref, request.instance_id, target.name or None, request.metric.key,
             to_db(request.start), to_db(request.end), request.hours, returned_points, to_db(fetched_at)),
        )


def record_collection(
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
            "INSERT INTO ec2_metric_collections (run_id, resource_ref, instance_id, instance_name, metric, namespace, "
            "metric_name, statistic, period_seconds, window_start, window_end, expected_points, requested_points, "
            "fetched_points, collected_points) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (run_id, target.ref, series.instance_id, target.name or None, series.metric.key,
             series.metric.namespace, series.metric.metric_name, STATISTIC, int(PERIOD.total_seconds()),
             to_db(series.window_start), to_db(series.window_end), series.expected_points, requested_points,
             fetched_points, series.collected_points),
        )


def load_series(
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
            "FROM ec2_metric_datapoints d JOIN resources r ON r.id = d.resource_ref "
            "WHERE r.account_ref = %s AND r.region = %s AND r.service = %s AND r.resource_type = %s "
            f"AND r.resource_id IN ({placeholders}) AND d.period_start >= %s AND d.period_start < %s "
            "ORDER BY d.period_start",
            [instance_scope.account_ref, instance_scope.region, instance_scope.service,
             instance_scope.resource_type, *ids, to_db(start), to_db(end)],
        )
        rows = cursor.fetchall()

    points: dict[SeriesKey, list[Datapoint]] = defaultdict(list)
    for instance_id, metric_key, period_start, average in rows:
        points[(instance_id, metric_key)].append(Datapoint(from_db(period_start), average))

    return [
        MetricSeries(instance_id, metric, start, end, tuple(points[(instance_id, metric.key)]))
        for instance_id in ids
        for metric in METRICS
    ]

def configuration_effective_since(
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
            "WHERE r.account_ref = %s AND r.region = %s AND r.service = %s AND r.resource_type = %s "
            f"AND r.resource_id IN ({placeholders}) AND c.change_type = 'modified' ORDER BY c.detected_at",
            [instance_scope.account_ref, instance_scope.region, instance_scope.service,
             instance_scope.resource_type, *ids],
        )
        for instance_id, detected_at, field_changes in cursor.fetchall():
            if any(item["field"] in SPEC_FIELDS for item in json.loads(field_changes)):
                since[instance_id] = from_db(detected_at)
    return since


def load_points_since(
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
            "FROM ec2_metric_datapoints d JOIN resources r ON r.id = d.resource_ref "
            "WHERE r.account_ref = %s AND r.region = %s AND r.service = %s AND r.resource_type = %s "
            f"AND r.resource_id IN ({placeholders}) ORDER BY d.period_start",
            [instance_scope.account_ref, instance_scope.region, instance_scope.service,
             instance_scope.resource_type, *ids],
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
            f"INSERT INTO ec2_metric_cumulative ({names}) VALUES ({placeholders}) AS new "
            f"ON DUPLICATE KEY UPDATE {updates}",
            tuple(columns.values()),
        )