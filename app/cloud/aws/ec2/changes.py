"""EC2 change detection: compare the tracked specs of instances between two points in time.

Pure logic with no storage or AWS calls: analysis.py loads the stored baseline,
takes the current snapshots and uses compare() to find what changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from rich.markup import escape

from app.auth.common import console
from app.cloud.aws.ec2.configuration import InstanceConfiguration
from app.cloud.display import NOT_SET, field, field_list

CHANGE_INDENT = 6

# Snapshot attribute -> label shown in the output, in display order.
TRACKED_FIELDS = {
    "name": "Name",
    "state": "Status",
    "instance_type": "Instance type",
    "family": "Family",
    "size": "Size",
    "vcpus": "vCPUs",
    "memory_gib": "Memory",
    "architecture": "Architecture",
    "burstable": "Burstable",
    "platform": "Platform",
    "lifecycle": "Lifecycle",
    "tenancy": "Tenancy",
    "ebs_optimized": "EBS optimized",
    "detailed_monitoring": "Detailed monitoring",
    "storage_gib": "Total storage",
    "volumes": "Volumes",
}

# Changes to these fields are stored but do not affect utilization, so they do not
# restart the evaluation window.
COSMETIC_FIELDS = frozenset({"name", "detailed_monitoring"})

KIND_STYLES = {
    "added": ("green", "+"),
    "removed": ("red", "−"),
    "modified": ("yellow", "●"),
}


@dataclass(frozen=True)
class InstanceSnapshot:
    """The tracked specs of one instance at one moment."""

    instance_id: str
    name: str
    state: str
    instance_type: str
    family: str
    size: str
    vcpus: int | None
    memory_gib: float | None
    architecture: str
    burstable: bool | None
    platform: str
    lifecycle: str
    tenancy: str
    ebs_optimized: bool
    detailed_monitoring: bool
    storage_gib: int
    volumes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable copy for storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> InstanceSnapshot:
        """Rebuild a snapshot created by to_dict()."""
        return cls(**{**data, "volumes": tuple(data["volumes"])})


@dataclass(frozen=True)
class FieldChange:
    """One tracked field whose value changed."""

    field: str
    before: Any
    after: Any

    @property
    def label(self) -> str:
        return TRACKED_FIELDS[self.field]


@dataclass(frozen=True)
class InstanceChange:
    """An instance that was added, removed or modified between two points in time."""

    kind: str
    before: InstanceSnapshot | None
    after: InstanceSnapshot | None
    fields: tuple[FieldChange, ...] = ()

    @property
    def current(self) -> InstanceSnapshot:
        """The latest known snapshot of the instance."""
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


def snapshot_instance(config: InstanceConfiguration) -> InstanceSnapshot:
    """Reduce a full configuration to the specs that are tracked for changes."""
    family, _, size = config.instance_type.partition(".")
    spec = config.type_spec

    return InstanceSnapshot(
        instance_id=config.instance.instance_id,
        name=config.instance.name,
        state=config.instance.state,
        instance_type=config.instance_type,
        family=family,
        size=size,
        vcpus=spec.vcpus if spec else None,
        memory_gib=spec.memory_gib if spec else None,
        architecture=config.architecture,
        burstable=spec.burstable if spec else None,
        platform=config.platform,
        lifecycle=config.lifecycle,
        tenancy=config.tenancy,
        ebs_optimized=config.ebs_optimized,
        detailed_monitoring=config.detailed_monitoring,
        storage_gib=sum(volume.size_gib or 0 for volume in config.volumes),
        volumes=tuple(
            f"{volume.device} {volume.volume_id} {volume.size_gib or '?'} GiB {volume.volume_type}".strip()
            for volume in config.volumes
        ),
    )


def field_changes(before: InstanceSnapshot, after: InstanceSnapshot) -> tuple[FieldChange, ...]:
    """Return every tracked field whose value differs, in display order."""
    return tuple(
        FieldChange(name, getattr(before, name), getattr(after, name))
        for name in TRACKED_FIELDS
        if getattr(before, name) != getattr(after, name)
    )


def compare(
    before: Mapping[str, InstanceSnapshot],
    after: Mapping[str, InstanceSnapshot],
) -> list[InstanceChange]:
    """Return the instances added, removed or modified between two sets of snapshots."""
    changes: list[InstanceChange] = []

    for instance_id in sorted(before.keys() | after.keys()):
        old, new = before.get(instance_id), after.get(instance_id)
        if old is None:
            changes.append(InstanceChange("added", None, new))
        elif new is None:
            changes.append(InstanceChange("removed", old, None))
        elif changed := field_changes(old, new):
            changes.append(InstanceChange("modified", old, new, changed))

    return changes


def _format(name: str, value: Any) -> str:
    """Format a tracked value for display."""
    if value is None or value == "" or value == ():
        return NOT_SET
    if isinstance(value, bool):
        return "yes" if value else "no"
    if name in ("memory_gib", "storage_gib"):
        return f"{value:g} GiB"
    if isinstance(value, (tuple, list)):
        return ", ".join(value)
    return str(value)


def summary(snapshot: InstanceSnapshot) -> str:
    """Return e.g. 't3.medium · 2 vCPU · 4 GiB · running'."""
    parts = [
        snapshot.instance_type,
        f"{snapshot.vcpus} vCPU" if snapshot.vcpus else "",
        _format("memory_gib", snapshot.memory_gib) if snapshot.memory_gib else "",
        snapshot.state,
    ]
    return " · ".join(part for part in parts if part)


def instance_label(snapshot: InstanceSnapshot) -> str:
    """Return rich markup for 'name · i-...' (unnamed instances shown dimmed)."""
    name = escape(snapshot.name) if snapshot.name else "[dim](no name)[/]"
    return f"[bold]{name}[/] [dim]·[/] {snapshot.instance_id}"


def print_change(change: InstanceChange) -> None:
    """Print one added, removed or modified instance and what changed."""
    style, mark = KIND_STYLES[change.kind]
    console.print(f"  [{style}]{mark}[/] {instance_label(change.current)} [dim]·[/] [{style}]{change.kind}[/]")

    if change.kind != "modified":
        field("Specs", summary(change.current), indent=CHANGE_INDENT)
        return

    for item in change.fields:
        if item.field == "volumes":
            removed = [f"− {volume}" for volume in item.before if volume not in item.after]
            added = [f"+ {volume}" for volume in item.after if volume not in item.before]
            field_list(item.label, removed + added, indent=CHANGE_INDENT)
        else:
            field(
                item.label,
                f"{_format(item.field, item.before)} → {_format(item.field, item.after)}",
                indent=CHANGE_INDENT,
            )