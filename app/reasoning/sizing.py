"""Deterministic sizing: required capacity, validated candidate sizes and the rule-based decision.

Capacity (from cumulative p95 utilization and safety factors):
    required vCPU    = current vCPU x min(CPU p95 x CPU safety factor, 100%)
    required memory  = current memory x memory p95 x memory safety factor

Candidates are every size of the instance's own family (same architecture and behaviour),
read from EC2 DescribeInstanceTypes. A candidate is safe only if it provides at least the
required vCPU and memory. The smallest safe size decides:
    smaller than current  -> DOWNSIZE_CANDIDATE
    the current size      -> KEEP_CURRENT
    larger than current   -> UPSCALE_CANDIDATE
    no safe size          -> INVESTIGATE

When real on-demand prices are known for the current size and every safe size,
apply_prices refines this to the cheapest safe size (ties go to the smaller size), and a
change is suggested only if it is cheaper, or required because the current size is unsafe.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.auth import aws_auth
from app.cloud.aws.backoff import with_backoff
from app.cloud.aws.errors import aws_errors
from app.reasoning.config import CPU_SAFETY_FACTOR, MEMORY_SAFETY_FACTOR


@dataclass(frozen=True)
class InstanceSize:
    instance_type: str
    vcpus: int
    memory_mib: int

    @property
    def order(self) -> tuple[int, int, str]:
        return (self.vcpus, self.memory_mib, self.instance_type)


@dataclass(frozen=True)
class Capacity:
    """What the observed workload needs, with safety headroom."""

    cpu_p95: float
    memory_p95: float
    cpu_required_percent: float
    cpu_required_vcpus: float
    memory_required_mib: float
    cpu_pressure: str
    memory_pressure: str


@dataclass(frozen=True)
class Candidate:
    size: InstanceSize
    safe: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SizingResult:
    capacity: Capacity
    candidates: tuple[Candidate, ...]
    decision: str
    recommended: InstanceSize | None


def pressure(p95: float) -> str:
    """Classify resource pressure from p95 utilization."""
    if p95 >= 90.0:
        return "CRITICAL"
    if p95 >= 80.0:
        return "HIGH"
    if p95 >= 60.0:
        return "MEDIUM"
    if p95 >= 40.0:
        return "LOW"
    return "VERY_LOW"


def capacity_for(current: InstanceSize, cpu_p95: float, memory_p95: float) -> Capacity:
    cpu_required_percent = min(cpu_p95 * CPU_SAFETY_FACTOR, 100.0)
    return Capacity(
        cpu_p95=cpu_p95,
        memory_p95=memory_p95,
        cpu_required_percent=cpu_required_percent,
        cpu_required_vcpus=current.vcpus * cpu_required_percent / 100.0,
        memory_required_mib=current.memory_mib * (memory_p95 / 100.0) * MEMORY_SAFETY_FACTOR,
        cpu_pressure=pressure(cpu_p95),
        memory_pressure=pressure(memory_p95),
    )


@lru_cache(maxsize=64)
def family_sizes(region: str, family: str) -> tuple[InstanceSize, ...]:
    """Every size of an instance family in the region (one DescribeInstanceTypes listing)."""
    client = aws_auth.get_client("ec2", region=region)

    def list_sizes() -> list[InstanceSize]:
        sizes = []
        for page in client.get_paginator("describe_instance_types").paginate(
            Filters=[{"Name": "instance-type", "Values": [f"{family}.*"]}]
        ):
            for item in page["InstanceTypes"]:
                sizes.append(InstanceSize(
                    item["InstanceType"], item["VCpuInfo"]["DefaultVCpus"], item["MemoryInfo"]["SizeInMiB"]
                ))
        return sizes

    with aws_errors("ec2:DescribeInstanceTypes"):
        sizes = with_backoff(list_sizes, label="ec2:DescribeInstanceTypes")
    return tuple(sorted(sizes, key=lambda size: size.order))


def validate(size: InstanceSize, capacity: Capacity) -> Candidate:
    cpu_ok = size.vcpus >= capacity.cpu_required_vcpus
    memory_ok = size.memory_mib >= capacity.memory_required_mib
    reasons = [
        f"{size.vcpus} vCPU {'covers' if cpu_ok else 'is below'} the required {capacity.cpu_required_vcpus:.2f} vCPU",
        f"{size.memory_mib} MiB {'covers' if memory_ok else 'is below'} the required "
        f"{capacity.memory_required_mib:.0f} MiB",
    ]
    return Candidate(size, cpu_ok and memory_ok, tuple(reasons))


def size_instance(
    region: str,
    current: InstanceSize,
    cpu_p95: float,
    memory_p95: float,
) -> SizingResult:
    """Compute capacity, validate every family size and take the rule-based decision."""
    family = current.instance_type.split(".", 1)[0]
    capacity = capacity_for(current, cpu_p95, memory_p95)
    sizes = family_sizes(region, family) or (current,)
    candidates = tuple(validate(size, capacity) for size in sizes)
    safe = [candidate.size for candidate in candidates if candidate.safe]

    if not safe:
        return SizingResult(capacity, candidates, "INVESTIGATE", None)
    smallest = min(safe, key=lambda size: size.order)
    if smallest.instance_type == current.instance_type:
        return SizingResult(capacity, candidates, "KEEP_CURRENT", None)
    if smallest.order < current.order:
        return SizingResult(capacity, candidates, "DOWNSIZE_CANDIDATE", smallest)
    return SizingResult(capacity, candidates, "UPSCALE_CANDIDATE", smallest)


def apply_prices(result: SizingResult, current: InstanceSize, hourly: dict[str, float]) -> SizingResult:
    """Refine the decision with real hourly prices; unchanged when any needed price is missing."""
    safe = [candidate.size for candidate in result.candidates if candidate.safe]
    if not safe or current.instance_type not in hourly or any(size.instance_type not in hourly for size in safe):
        return result
    cheapest = min(safe, key=lambda size: (hourly[size.instance_type], size.order))
    current_is_safe = any(size.instance_type == current.instance_type for size in safe)

    if cheapest.instance_type == current.instance_type:
        return SizingResult(result.capacity, result.candidates, "KEEP_CURRENT", None)
    if current_is_safe:
        if hourly[cheapest.instance_type] < hourly[current.instance_type]:
            return SizingResult(result.capacity, result.candidates, "DOWNSIZE_CANDIDATE", cheapest)
        return SizingResult(result.capacity, result.candidates, "KEEP_CURRENT", None)
    return SizingResult(result.capacity, result.candidates, "UPSCALE_CANDIDATE", cheapest)