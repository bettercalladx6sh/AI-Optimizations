"""Azure VM sizes: hardware specs and family members per location, from Compute Resource SKUs.

One Resource SKUs listing per location (cached for the run) gives every VM size with its
vCPUs, memory, CPU architecture and family (e.g. standardDSv3Family). A size is a valid
rightsizing candidate only when it is offered to this subscription in that location (no
NotAvailableForSubscription restriction) and is not a constrained-vCPU size (e.g.
Standard_E4-2s_v3, which is billed as the full size).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.cloud.azure import rest

API_VERSION = "2021-07-01"
BURSTABLE = re.compile(r"^Standard_B\d", re.IGNORECASE)


@dataclass(frozen=True)
class VmSize:
    """Hardware of one VM size in one location."""

    name: str
    family: str
    vcpus: int
    memory_gib: float
    architecture: str
    burstable: bool
    restricted: bool
    constrained: bool

    @property
    def memory_mib(self) -> int:
        return int(round(self.memory_gib * 1024))

    @property
    def order(self) -> tuple[int, int, str]:
        return (self.vcpus, self.memory_mib, self.name)


def _to_size(item: dict[str, Any]) -> VmSize | None:
    capabilities = {entry["name"]: entry["value"] for entry in item.get("capabilities", [])}
    try:
        vcpus = int(capabilities["vCPUs"])
        memory = float(capabilities["MemoryGB"])
    except (KeyError, ValueError):
        return None
    available = capabilities.get("vCPUsAvailable")
    restricted = any(
        restriction.get("type") == "Location" and restriction.get("reasonCode") == "NotAvailableForSubscription"
        for restriction in item.get("restrictions", [])
    )
    return VmSize(
        name=item["name"],
        family=item.get("family", ""),
        vcpus=vcpus,
        memory_gib=memory,
        architecture=capabilities.get("CpuArchitectureType", "x64"),
        burstable=bool(BURSTABLE.match(item["name"])),
        restricted=restricted,
        constrained=available is not None and int(available) < vcpus,
    )


@lru_cache(maxsize=32)
def sizes(location: str) -> dict[str, VmSize]:
    """Every VM size in a location, keyed by lower-case name (Azure names are case-insensitive)."""
    found: dict[str, VmSize] = {}
    for item in rest.arm_list(
        f"{rest.subscription_path()}/providers/Microsoft.Compute/skus", API_VERSION,
        "Microsoft.Compute/skus/read", {"$filter": f"location eq '{location}'"},
    ):
        if item.get("resourceType") != "virtualMachines":
            continue
        size = _to_size(item)
        if size:
            found[size.name.lower()] = size
    return found


def size_spec(location: str, name: str) -> VmSize | None:
    return sizes(location).get(name.lower())


def family_members(location: str, name: str) -> tuple[VmSize, ...]:
    """Every size of the same family that this subscription can deploy in the location, smallest first."""
    current = size_spec(location, name)
    if current is None or not current.family:
        return ()
    members = [
        size for size in sizes(location).values()
        if size.family == current.family and size.architecture == current.architecture
        and not size.restricted and not size.constrained
    ]
    return tuple(sorted(members, key=lambda size: size.order))
