"""Azure VM storage: write discovered VMs into the local analysis database.

Each VM is stored as a generic resource (resources, resource_snapshots, resource_tags,
resource_changes via app.db.resources, resource_id = vmId) plus Azure-specific typed rows
(azure_vms, azure_vm_disks). Hourly metrics, checked ranges, per-run collections and
cumulative summaries go to the azure_vm_metric_* tables through the shared app/db/metrics.py.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from pymysql.connections import Connection

from app.cloud.azure.vm.changes import SPEC_FIELDS, FieldChange, VmChange, VmSnapshot
from app.cloud.azure.vm.inventory import VmConfiguration
from app.cloud.azure.vm.metrics import CPU, MEMORY_PLATFORM
from app.db import resources
from app.db.metrics import MetricTables, MetricTarget
from app.db.resources import ChangeRecord, StoredResource, to_db

__all__ = ["PROVIDER", "RESOURCE_TYPE", "SERVICE", "MetricTarget", "record_change", "save_vm", "scope",
           "tables", "to_vm_change"]

PROVIDER = "azure"
SERVICE = "vm"
RESOURCE_TYPE = "virtual_machine"
ALL_LOCATIONS = "all"


def scope(account_ref: int, location: str | None) -> resources.Scope:
    """The storage scope for VMs in one subscription and location (or every location)."""
    return resources.Scope(account_ref, location or ALL_LOCATIONS, SERVICE, RESOURCE_TYPE)


def tables(metrics: tuple[Any, ...] = (CPU, MEMORY_PLATFORM)) -> MetricTables:
    """The Azure VM metric tables for the active metric set (memory source depends on .env)."""
    return MetricTables("azure_vm", metrics, SPEC_FIELDS)


def save_vm(
    connection: Connection,
    vm_scope: resources.Scope,
    run_id: int,
    seen_at: datetime,
    config: VmConfiguration,
    snapshot: VmSnapshot,
    stored: StoredResource | None,
    changed: bool,
) -> tuple[int, int]:
    """Store one VM seen in this run; return (resource row ID, current snapshot ID)."""
    resource = resources.save_resource(
        connection, vm_scope, snapshot.instance_id, snapshot.name, snapshot.state, run_id, seen_at, changed
    )
    snapshot_id = resources.save_snapshot(
        connection, resource, run_id, seen_at, snapshot.to_dict(), asdict(config), stored
    )
    resources.replace_tags(connection, resource, ((key[:128], value[:256]) for key, value in config.tags))
    _save_details(connection, resource, config, snapshot, seen_at)
    return resource, snapshot_id


def _save_details(connection: Connection, resource: int, config: VmConfiguration, snapshot: VmSnapshot,
                  updated_at: datetime) -> None:
    """Replace the typed azure_vms and azure_vm_disks rows with the current configuration."""
    spec = config.size_spec
    columns = {
        "resource_ref": resource,
        "arm_id": config.vm.resource_id,
        "resource_group": config.vm.resource_group,
        "vm_size": config.vm_size,
        "family": spec.family if spec else None,
        "vcpus": spec.vcpus if spec else None,
        "memory_gib": spec.memory_gib if spec else None,
        "architecture": spec.architecture if spec else None,
        "burstable": spec.burstable if spec else None,
        "os_type": config.os_type or None,
        "image": config.image or None,
        "marketplace_plan": config.marketplace_plan or None,
        "license_type": config.license_type or None,
        "priority": config.priority or None,
        "eviction_policy": config.eviction_policy or None,
        "location": config.vm.location,
        "zone": config.zone or None,
        "availability_set": config.availability_set or None,
        "scale_set": config.scale_set or None,
        "dedicated_host": config.dedicated_host,
        "boot_diagnostics": config.boot_diagnostics,
        "storage_gib": snapshot.storage_gib,
        "time_created": to_db(config.time_created),
        "updated_at": to_db(updated_at),
    }
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"{name} = new.{name}" for name in columns if name != "resource_ref")

    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO azure_vms ({names}) VALUES ({placeholders}) AS new ON DUPLICATE KEY UPDATE {updates}",
            tuple(columns.values()),
        )
        cursor.execute("DELETE FROM azure_vm_disks WHERE resource_ref = %s", (resource,))
        disks = [
            (resource, d.name, d.role, d.lun, d.size_gib, d.sku or None, d.iops, d.throughput_mbps,
             d.encryption or None, d.delete_option or None)
            for d in config.disks
        ]
        if disks:
            cursor.executemany(
                "INSERT INTO azure_vm_disks (resource_ref, disk_name, role, lun, size_gib, sku, iops, "
                "throughput_mbps, encryption, delete_option) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                disks,
            )


def record_change(
    connection: Connection,
    resource: int,
    run_id: int,
    detected_at: datetime,
    change: VmChange,
    before_snapshot_id: int | None,
    after_snapshot_id: int | None,
) -> None:
    """Store one detected VM change."""
    resources.record_change(
        connection, resource, run_id, detected_at, change.kind, change.significant,
        ({"field": item.field, "before": item.before, "after": item.after} for item in change.fields),
        before_snapshot_id, after_snapshot_id,
    )


def to_vm_change(record: ChangeRecord) -> VmChange:
    """Rebuild a VmChange from a stored change record."""
    return VmChange(
        kind=record.change_type,
        before=VmSnapshot.from_dict(record.before) if record.before else None,
        after=VmSnapshot.from_dict(record.after) if record.after else None,
        fields=tuple(FieldChange(item["field"], item["before"], item["after"]) for item in record.field_changes),
    )
