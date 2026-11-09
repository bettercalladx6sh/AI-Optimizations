"""EC2 storage: write discovered instances into the analysis database.

Each instance is stored as a generic resource (resources, resource_snapshots,
resource_tags, resource_changes via app.db.resources) plus EC2-specific typed rows
(ec2_instances, ec2_volumes) for querying its current configuration.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from pymysql.connections import Connection

from app.cloud.aws.ec2.changes import FieldChange, InstanceChange, InstanceSnapshot
from app.cloud.aws.ec2.configuration import InstanceConfiguration
from app.db import resources
from app.db.resources import ChangeRecord, StoredResource, to_db, to_json

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