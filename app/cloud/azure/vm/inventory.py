"""Azure VM discovery and configuration: read-only listing of the VMs in the subscription.

Three Resource Manager listings cover every VM in the subscription:
    virtualMachines                  the VM models (size, OS, image, disks, tags, ...)
    virtualMachines?statusOnly=true  each VM's power state (running, deallocated, ...)
    disks                            managed disk sizes, SKUs and performance
VM size specs (vCPUs, memory, family) come from Compute Resource SKUs (skus.py).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from rich.markup import escape

from app.auth.common import console
from app.cloud.azure import rest
from app.cloud.azure.rest import AzureApiError
from app.cloud.azure.vm import skus
from app.cloud.azure.vm.skus import VmSize
from app.cloud.display import field, field_list, print_header, section

VM_API_VERSION = "2024-07-01"
DISK_API_VERSION = "2024-03-02"
FIELD_INDENT = 4

POWER_STATES = ("running", "starting", "stopping", "stopped", "deallocating", "deallocated", "unknown")
STATE_STYLES = {
    "running": "green",
    "stopped": "red",
    "deallocated": "red",
    "starting": "yellow",
    "stopping": "yellow",
    "deallocating": "yellow",
    "unknown": "dim",
}
ENCRYPTION_LABELS = {
    "EncryptionAtRestWithPlatformKey": "platform-managed key",
    "EncryptionAtRestWithCustomerKey": "customer-managed key",
    "EncryptionAtRestWithPlatformAndCustomerKeys": "double encryption",
}
NOT_FOUND_HINTS = (
    "Use the VM name, its vmId or its resource ID",
    "Check --location and AZURE_SUBSCRIPTION_ID",
)


@dataclass(frozen=True)
class AzureVm:
    """The VM fields this app works with. instance_id is the VM's immutable vmId."""

    instance_id: str
    name: str
    state: str
    resource_group: str
    location: str
    resource_id: str


@dataclass(frozen=True)
class DiskSpec:
    """One managed disk attached to a VM."""

    name: str
    role: str                     # os or data
    lun: int | None
    size_gib: int | None
    sku: str
    iops: int | None
    throughput_mbps: int | None
    encryption: str
    delete_option: str


@dataclass(frozen=True)
class VmConfiguration:
    """Full configuration of one Azure VM."""

    vm: AzureVm
    vm_size: str
    size_spec: VmSize | None
    os_type: str
    image: str
    marketplace_plan: str
    license_type: str
    priority: str
    eviction_policy: str
    zone: str
    availability_set: str
    scale_set: str
    dedicated_host: bool
    boot_diagnostics: bool
    time_created: datetime | None
    disks: tuple[DiskSpec, ...]
    tags: tuple[tuple[str, str], ...]

    @property
    def instance_id(self) -> str:
        return self.vm.instance_id


def _resource_group(resource_id: str) -> str:
    parts = resource_id.split("/")
    lowered = [part.lower() for part in parts]
    return parts[lowered.index("resourcegroups") + 1] if "resourcegroups" in lowered else ""


def _last(resource_id: str | None) -> str:
    return resource_id.rstrip("/").rsplit("/", 1)[-1] if resource_id else ""


def _power_state(statuses: Iterable[dict[str, Any]]) -> str:
    for status in statuses:
        code = status.get("code", "")
        if code.startswith("PowerState/"):
            state = code.split("/", 1)[1].lower()
            return state if state in POWER_STATES else "unknown"
    return "unknown"


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def describe_vms(location: str | None = None) -> list[dict[str, Any]]:
    """Raw VM records of the subscription (optionally one location), each with its power state."""
    base = f"{rest.subscription_path()}/providers/Microsoft.Compute/virtualMachines"
    action = "Microsoft.Compute/virtualMachines/read"
    models = list(rest.arm_list(base, VM_API_VERSION, action))
    states = {
        item["id"].lower(): _power_state(item.get("properties", {}).get("instanceView", {}).get("statuses", []))
        for item in rest.arm_list(base, VM_API_VERSION, action, {"statusOnly": "true"})
    }
    records = []
    for model in models:
        if location and model.get("location", "").lower() != location.lower():
            continue
        records.append({**model, "_state": states.get(model["id"].lower(), "unknown")})
    return records


def _disk_details(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Managed disks of the subscription keyed by lower-case resource ID (one listing, only when needed)."""
    if not records:
        return {}
    return {
        disk["id"].lower(): disk
        for disk in rest.arm_list(f"{rest.subscription_path()}/providers/Microsoft.Compute/disks",
                                  DISK_API_VERSION, "Microsoft.Compute/disks/read")
    }


def to_vm(raw: dict[str, Any]) -> AzureVm:
    properties = raw.get("properties", {})
    return AzureVm(
        instance_id=(properties.get("vmId") or raw["id"]).lower(),
        name=raw.get("name", ""),
        state=raw.get("_state", "unknown"),
        resource_group=_resource_group(raw["id"]),
        location=raw.get("location", ""),
        resource_id=raw["id"],
    )


def _disk(entry: dict[str, Any], role: str, details: dict[str, dict[str, Any]]) -> DiskSpec:
    managed = entry.get("managedDisk") or {}
    detail = details.get((managed.get("id") or "").lower(), {})
    detail_properties = detail.get("properties", {})
    return DiskSpec(
        name=entry.get("name") or _last(managed.get("id")),
        role=role,
        lun=entry.get("lun"),
        size_gib=detail_properties.get("diskSizeGB") or entry.get("diskSizeGB"),
        sku=(detail.get("sku") or {}).get("name") or managed.get("storageAccountType") or "",
        iops=detail_properties.get("diskIOPSReadWrite"),
        throughput_mbps=detail_properties.get("diskMBpsReadWrite"),
        encryption=(detail_properties.get("encryption") or {}).get("type", ""),
        delete_option=entry.get("deleteOption", ""),
    )


def _image(storage: dict[str, Any]) -> str:
    reference = storage.get("imageReference") or {}
    if reference.get("publisher"):
        return ":".join(reference.get(key, "") for key in ("publisher", "offer", "sku"))
    if reference.get("id") or reference.get("sharedGalleryImageId") or reference.get("communityGalleryImageId"):
        return "custom image"
    return "attached OS disk" if (storage.get("osDisk") or {}).get("createOption") == "Attach" else ""


def to_configuration(raw: dict[str, Any], details: dict[str, dict[str, Any]]) -> VmConfiguration:
    vm = to_vm(raw)
    properties = raw.get("properties", {})
    storage = properties.get("storageProfile", {})
    os_disk = storage.get("osDisk") or {}
    vm_size = (properties.get("hardwareProfile") or {}).get("vmSize", "")
    plan = raw.get("plan") or {}
    disks = [_disk(os_disk, "os", details)] if os_disk else []
    disks += [_disk(entry, "data", details) for entry in sorted(storage.get("dataDisks", []), key=lambda d: d.get("lun", 0))]
    return VmConfiguration(
        vm=vm,
        vm_size=vm_size,
        size_spec=skus.size_spec(vm.location, vm_size) if vm_size and vm.location else None,
        os_type=os_disk.get("osType", ""),
        image=_image(storage),
        marketplace_plan="/".join(plan.get(key, "") for key in ("publisher", "product", "name")) if plan else "",
        license_type=properties.get("licenseType", ""),
        priority=properties.get("priority") or "Regular",
        eviction_policy=properties.get("evictionPolicy", ""),
        zone=",".join(raw.get("zones") or []),
        availability_set=_last((properties.get("availabilitySet") or {}).get("id")),
        scale_set=_last((properties.get("virtualMachineScaleSet") or {}).get("id")),
        dedicated_host=bool(properties.get("host") or properties.get("hostGroup")),
        boot_diagnostics=bool(((properties.get("diagnosticsProfile") or {}).get("bootDiagnostics") or {}).get("enabled")),
        time_created=_time(properties.get("timeCreated")),
        disks=tuple(disks),
        tags=tuple(sorted((raw.get("tags") or {}).items())),
    )


def sort_key(vm: AzureVm) -> tuple[bool, str, str]:
    """Running first; then by name."""
    return (vm.state != "running", vm.name.lower(), vm.instance_id)


def build_configurations(records: list[dict[str, Any]]) -> list[VmConfiguration]:
    """Turn raw VM records into full configurations (size specs and disks added)."""
    details = _disk_details(records)
    configurations = [to_configuration(raw, details) for raw in records]
    return sorted(configurations, key=lambda config: sort_key(config.vm))


def matches(raw: dict[str, Any], resource: str) -> bool:
    wanted = resource.lower()
    return wanted in (
        raw.get("name", "").lower(),
        raw["id"].lower(),
        (raw.get("properties", {}).get("vmId") or "").lower(),
    )


def get_configurations(location: str | None = None, resource: str | None = None) -> list[VmConfiguration]:
    """Full configurations of every VM, or of one VM matched by name, vmId or resource ID."""
    records = describe_vms(location)
    if resource:
        records = [raw for raw in records if matches(raw, resource)]
        if not records:
            raise AzureApiError(f"no VM found matching '{resource}'", NOT_FOUND_HINTS)
    return build_configurations(records)


def list_vms(location: str | None = None) -> list[AzureVm]:
    return sorted((to_vm(raw) for raw in describe_vms(location)), key=sort_key)


# ---------------------------------------------------------------- output


def state_summary(vms: Iterable[AzureVm]) -> str:
    """Return e.g. '2 running, 1 deallocated'."""
    counts = Counter(vm.state for vm in vms)
    return ", ".join(f"{count} {state}" for state, count in counts.most_common())


def print_vm_heading(vm: AzureVm) -> None:
    style = STATE_STYLES.get(vm.state, "white")
    console.print(f"[{style}]●[/] [bold]{escape(vm.name)}[/] [dim]· {escape(vm.resource_group)}[/]")


def print_vms(vms: list[AzureVm], scope: str) -> None:
    print_header("Azure VMs", scope, len(vms), state_summary(vms))
    if not vms:
        console.print("  [dim]No VMs found.[/]")
        console.print()
        return
    for vm in vms:
        print_vm_heading(vm)
        field("VM ID", vm.instance_id)
        field("Status", vm.state, STATE_STYLES.get(vm.state, ""))
        field("Location", vm.location)
        field("Resource group", vm.resource_group)
        console.print()


def _yes_no(value: bool | None) -> str:
    return "" if value is None else ("yes" if value else "no")


def _created(moment: datetime | None) -> str:
    if not moment:
        return ""
    days = (datetime.now(timezone.utc) - moment).days
    age = "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago"
    return f"{moment:%Y-%m-%d %H:%M} UTC ({age})"


def _disk_lines(disk: DiskSpec) -> tuple[str, str]:
    """Two lines: 'os · vm-01_OsDisk' and '128 GiB Premium_LRS · 500 IOPS · 100 MB/s'."""
    parts = [f"{disk.size_gib} GiB {disk.sku}".strip() if disk.size_gib else disk.sku]
    if disk.iops:
        parts.append(f"{disk.iops} IOPS")
    if disk.throughput_mbps:
        parts.append(f"{disk.throughput_mbps} MB/s")
    if disk.encryption:
        parts.append(ENCRYPTION_LABELS.get(disk.encryption, disk.encryption))
    role = disk.role if disk.lun is None else f"{disk.role} lun {disk.lun}"
    return f"{role} · {disk.name}", f"  {' · '.join(part for part in parts if part)}"


def print_configuration(config: VmConfiguration) -> None:
    vm, spec, indent = config.vm, config.size_spec, FIELD_INDENT
    print_vm_heading(vm)

    section("Overview")
    field("VM ID", vm.instance_id, indent=indent)
    field("Status", vm.state, STATE_STYLES.get(vm.state, ""), indent=indent)
    field("Created", _created(config.time_created), indent=indent)
    field("Resource group", vm.resource_group, indent=indent)
    field_list("Tags", (f"{key} = {value}" for key, value in config.tags), indent=indent)

    section("Compute")
    field("VM size", config.vm_size, "bold", indent=indent)
    field("Family", spec.family if spec else "", indent=indent)
    field("vCPUs", spec.vcpus if spec else "", indent=indent)
    field("Memory", f"{spec.memory_gib:g} GiB" if spec else "", indent=indent)
    field("Burstable", _yes_no(spec.burstable) if spec else "", indent=indent)
    field("Architecture", spec.architecture if spec else "", indent=indent)
    field("OS", config.os_type, indent=indent)
    field("Image", config.image, indent=indent)
    field("Marketplace plan", config.marketplace_plan, indent=indent)
    field("Licence", config.license_type or "included (pay as you go)", indent=indent)
    field("Priority", config.priority + (f" · evict {config.eviction_policy}" if config.eviction_policy else ""),
          indent=indent)

    section("Placement")
    field("Location", vm.location, indent=indent)
    field("Zone", config.zone, indent=indent)
    field("Availability set", config.availability_set, indent=indent)
    field("Scale set", config.scale_set, indent=indent)
    field("Dedicated host", _yes_no(config.dedicated_host), indent=indent)

    section("Storage")
    field_list("Disks", (line for disk in config.disks for line in _disk_lines(disk)), indent=indent)
    field("Boot diagnostics", _yes_no(config.boot_diagnostics), indent=indent)


def print_configurations(configs: list[VmConfiguration], scope: str) -> None:
    print_header("Azure VM configuration", scope, len(configs), state_summary(config.vm for config in configs))
    if not configs:
        console.print("  [dim]No VMs found.[/]")
        console.print()
        return
    for config in configs:
        print_configuration(config)
        console.print()
