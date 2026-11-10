"""Azure VM change detection: compare the tracked specs of VMs between two points in time.

Pure logic with no storage or Azure calls, like the EC2 version: analysis.py loads the stored
baseline, takes the current snapshots and uses compare() to find what changed. A resize, a
disk or OS change restarts the evaluation window; a rename or a stop/start does not.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from rich.markup import escape

from app.auth.common import console
from app.cloud.azure.vm.inventory import VmConfiguration
from app.cloud.display import NOT_SET, field, field_list

CHANGE_INDENT = 6

# Snapshot attribute -> label shown in the output, in display order.
TRACKED_FIELDS = {
    "name": "Name",
    "state": "Status",
    "vm_size": "VM size",
    "family": "Family",
    "vcpus": "vCPUs",
    "memory_gib": "Memory",
    "architecture": "Architecture",
    "burstable": "Burstable",
    "os_type": "OS",
    "image": "Image",
    "license_type": "Licence",
    "priority": "Priority",
    "location": "Location",
    "zone": "Zone",
    "storage_gib": "Total storage",
    "disks": "Disks",
    "boot_diagnostics": "Boot diagnostics",
}

# Stored, but they do not affect utilization, so they do not restart the evaluation window.
COSMETIC_FIELDS = frozenset({"name", "boot_diagnostics"})

# Fields that define a configuration version: the VM's specs.
SPEC_FIELDS = tuple(name for name in TRACKED_FIELDS if name not in COSMETIC_FIELDS | {"state"})

KIND_STYLES = {
    "added": ("green", "+"),
    "removed": ("red", "−"),
    "modified": ("yellow", "●"),
}


@dataclass(frozen=True)
class VmSnapshot:
    """The tracked specs of one VM at one moment. instance_id is the VM's vmId."""

    instance_id: str
    name: str
    state: str
    vm_size: str
    family: str
    vcpus: int | None
    memory_gib: float | None
    architecture: str
    burstable: bool | None
    os_type: str
    image: str
    license_type: str
    priority: str
    location: str
    zone: str
    storage_gib: int
    disks: tuple[str, ...]
    boot_diagnostics: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VmSnapshot:
        return cls(**{**data, "disks": tuple(data["disks"])})


@dataclass(frozen=True)
class FieldChange:
    field: str
    before: Any
    after: Any

    @property
    def label(self) -> str:
        return TRACKED_FIELDS[self.field]


@dataclass(frozen=True)
class VmChange:
    """A VM that was added, removed or modified between two points in time."""

    kind: str
    before: VmSnapshot | None
    after: VmSnapshot | None
    fields: tuple[FieldChange, ...] = ()

    @property
    def current(self) -> VmSnapshot:
        return self.after or self.before  # type: ignore[return-value]

    @property
    def instance_id(self) -> str:
        return self.current.instance_id

    @property
    def significant(self) -> bool:
        """True when the change can affect utilization and restarts the evaluation window."""
        if self.kind != "modified":
            return True
        return any(item.field not in COSMETIC_FIELDS for item in self.fields)


def configuration_version(snapshot: VmSnapshot) -> str:
    """SHA-256 of the VM's specs; changes only when size, OS, disks, ... change."""
    specs = {name: getattr(snapshot, name) for name in SPEC_FIELDS}
    return hashlib.sha256(json.dumps(specs, sort_keys=True, default=list).encode("utf-8")).hexdigest()


def snapshot_vm(config: VmConfiguration) -> VmSnapshot:
    """Reduce a full configuration to the specs that are tracked for changes."""
    spec = config.size_spec
    return VmSnapshot(
        instance_id=config.vm.instance_id,
        name=config.vm.name,
        state=config.vm.state,
        vm_size=config.vm_size,
        family=spec.family if spec else "",
        vcpus=spec.vcpus if spec else None,
        memory_gib=spec.memory_gib if spec else None,
        architecture=spec.architecture if spec else "",
        burstable=spec.burstable if spec else None,
        os_type=config.os_type,
        image=config.image,
        license_type=config.license_type,
        priority=config.priority,
        location=config.vm.location,
        zone=config.zone,
        storage_gib=sum(disk.size_gib or 0 for disk in config.disks),
        disks=tuple(f"{disk.role} {disk.name} {disk.size_gib or '?'} GiB {disk.sku}".strip() for disk in config.disks),
        boot_diagnostics=config.boot_diagnostics,
    )


def compare(before: Mapping[str, VmSnapshot], after: Mapping[str, VmSnapshot]) -> list[VmChange]:
    """Return the VMs added, removed or modified between two sets of snapshots."""
    changes: list[VmChange] = []
    for instance_id in sorted(before.keys() | after.keys()):
        old, new = before.get(instance_id), after.get(instance_id)
        if old is None:
            changes.append(VmChange("added", None, new))
        elif new is None:
            changes.append(VmChange("removed", old, None))
        else:
            fields = tuple(
                FieldChange(name, getattr(old, name), getattr(new, name))
                for name in TRACKED_FIELDS
                if getattr(old, name) != getattr(new, name)
            )
            if fields:
                changes.append(VmChange("modified", old, new, fields))
    return changes


def _format(name: str, value: Any) -> str:
    if value is None or value == "" or value == ():
        return NOT_SET
    if isinstance(value, bool):
        return "yes" if value else "no"
    if name in ("memory_gib", "storage_gib"):
        return f"{value:g} GiB"
    if isinstance(value, (tuple, list)):
        return ", ".join(value)
    return str(value)


def summary(snapshot: VmSnapshot) -> str:
    """Return e.g. 'Standard_D2s_v3 · 2 vCPU · 8 GiB · running'."""
    parts = [
        snapshot.vm_size,
        f"{snapshot.vcpus} vCPU" if snapshot.vcpus else "",
        _format("memory_gib", snapshot.memory_gib) if snapshot.memory_gib else "",
        snapshot.state,
    ]
    return " · ".join(part for part in parts if part)


def vm_label(snapshot: VmSnapshot) -> str:
    return f"[bold]{escape(snapshot.name)}[/] [dim]·[/] {snapshot.instance_id}"


def print_change(change: VmChange) -> None:
    style, mark = KIND_STYLES[change.kind]
    console.print(f"  [{style}]{mark}[/] {vm_label(change.current)} [dim]·[/] [{style}]{change.kind}[/]")
    if change.kind != "modified":
        field("Specs", summary(change.current), indent=CHANGE_INDENT)
        return
    for item in change.fields:
        if item.field == "disks":
            removed = [f"− {disk}" for disk in item.before if disk not in item.after]
            added = [f"+ {disk}" for disk in item.after if disk not in item.before]
            field_list(item.label, removed + added, indent=CHANGE_INDENT)
        else:
            field(item.label, f"{_format(item.field, item.before)} → {_format(item.field, item.after)}",
                  indent=CHANGE_INDENT)
