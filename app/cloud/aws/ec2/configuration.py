"""EC2 instance configuration: detailed, read-only specs for one or all instances."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from botocore.client import BaseClient

from app.auth import aws_auth
from app.auth.common import console
from app.cloud.aws.ec2.instances import (
    DEFAULT_STATES,
    INSTANCE_STATES,
    STATE_STYLES,
    Ec2Instance,
    describe_instances,
    print_instance_heading,
    sort_key,
    state_summary,
    to_instance,
)
from app.cloud.aws.errors import AwsApiError, aws_errors
from app.cloud.display import field, field_list, print_header, section

CHUNK_SIZE = 100
FIELD_INDENT = 4

NOT_FOUND_HINTS = (
    "Use an instance ID (i-...) or the exact value of the Name tag",
    "Check --region",
)


@dataclass(frozen=True)
class TypeSpec:
    """Hardware specs of an instance type, from describe_instance_types."""

    vcpus: int
    memory_gib: float
    network: str
    burstable: bool
    current_generation: bool


@dataclass(frozen=True)
class VolumeSpec:
    """One attached EBS volume."""

    volume_id: str
    device: str
    size_gib: int | None
    volume_type: str
    iops: int | None
    throughput: int | None
    encrypted: bool | None
    delete_on_termination: bool


@dataclass(frozen=True)
class InstanceConfiguration:
    """Full configuration of one EC2 instance."""

    instance: Ec2Instance
    instance_type: str
    type_spec: TypeSpec | None
    architecture: str
    platform: str
    lifecycle: str
    tenancy: str
    availability_zone: str
    subnet_id: str
    security_groups: tuple[str, ...]
    ami_id: str
    key_name: str
    iam_profile: str
    detailed_monitoring: bool
    ebs_optimized: bool
    root_device: str
    launch_time: datetime | None
    volumes: tuple[VolumeSpec, ...]
    tags: tuple[tuple[str, str], ...]


def _chunks(items: Iterable[str], size: int = CHUNK_SIZE) -> Iterator[list[str]]:
    """Split items into sorted lists of at most `size` (AWS request limits)."""
    ordered = sorted(items)
    for start in range(0, len(ordered), size):
        yield ordered[start:start + size]


def _type_specs(client: BaseClient, instance_types: Iterable[str]) -> dict[str, TypeSpec]:
    """Return hardware specs for each instance type."""
    specs: dict[str, TypeSpec] = {}
    paginator = client.get_paginator("describe_instance_types")

    for chunk in _chunks(instance_types):
        with aws_errors("ec2:DescribeInstanceTypes"):
            for page in paginator.paginate(InstanceTypes=chunk):
                for item in page["InstanceTypes"]:
                    specs[item["InstanceType"]] = TypeSpec(
                        vcpus=item["VCpuInfo"]["DefaultVCpus"],
                        memory_gib=item["MemoryInfo"]["SizeInMiB"] / 1024,
                        network=item.get("NetworkInfo", {}).get("NetworkPerformance", ""),
                        burstable=item.get("BurstablePerformanceSupported", False),
                        current_generation=item.get("CurrentGeneration", False),
                    )
    return specs


def _volume_details(client: BaseClient, volume_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Return raw describe_volumes records keyed by volume ID."""
    volumes: dict[str, dict[str, Any]] = {}
    paginator = client.get_paginator("describe_volumes")

    for chunk in _chunks(volume_ids):
        with aws_errors("ec2:DescribeVolumes"):
            for page in paginator.paginate(VolumeIds=chunk):
                for volume in page["Volumes"]:
                    volumes[volume["VolumeId"]] = volume
    return volumes


def _volumes(raw: Mapping[str, Any], details: Mapping[str, Mapping[str, Any]]) -> tuple[VolumeSpec, ...]:
    """Build VolumeSpec entries from an instance's block device mappings."""
    specs = []
    for mapping in raw.get("BlockDeviceMappings", []):
        ebs = mapping.get("Ebs")
        if not ebs:
            continue
        detail = details.get(ebs["VolumeId"], {})
        specs.append(VolumeSpec(
            volume_id=ebs["VolumeId"],
            device=mapping.get("DeviceName", ""),
            size_gib=detail.get("Size"),
            volume_type=detail.get("VolumeType", ""),
            iops=detail.get("Iops"),
            throughput=detail.get("Throughput"),
            encrypted=detail.get("Encrypted"),
            delete_on_termination=ebs.get("DeleteOnTermination", False),
        ))
    root = raw.get("RootDeviceName", "")
    return tuple(sorted(specs, key=lambda volume: (volume.device != root, volume.device)))


def _to_configuration(
    raw: Mapping[str, Any],
    type_specs: Mapping[str, TypeSpec],
    volume_details: Mapping[str, Mapping[str, Any]],
) -> InstanceConfiguration:
    """Convert one describe_instances record into an InstanceConfiguration."""
    placement = raw.get("Placement", {})
    profile_arn = raw.get("IamInstanceProfile", {}).get("Arn", "")
    root_name, root_type = raw.get("RootDeviceName", ""), raw.get("RootDeviceType", "")

    return InstanceConfiguration(
        instance=to_instance(raw),
        instance_type=raw["InstanceType"],
        type_spec=type_specs.get(raw["InstanceType"]),
        architecture=raw.get("Architecture", ""),
        platform=raw.get("PlatformDetails", ""),
        lifecycle=raw.get("InstanceLifecycle", "on-demand"),
        tenancy=placement.get("Tenancy", ""),
        availability_zone=placement.get("AvailabilityZone", ""),
        subnet_id=raw.get("SubnetId", ""),
        security_groups=tuple(
            f"{group['GroupName']} ({group['GroupId']})" for group in raw.get("SecurityGroups", [])
        ),
        ami_id=raw.get("ImageId", ""),
        key_name=raw.get("KeyName", ""),
        iam_profile=profile_arn.rsplit("/", 1)[-1],
        detailed_monitoring=raw.get("Monitoring", {}).get("State") == "enabled",
        ebs_optimized=raw.get("EbsOptimized", False),
        root_device=f"{root_name} ({root_type})" if root_name else root_type,
        launch_time=raw.get("LaunchTime"),
        volumes=_volumes(raw, volume_details),
        tags=tuple(sorted((tag["Key"], tag["Value"]) for tag in raw.get("Tags", []))),
    )

def get_configurations(
    region: str | None = None,
    resource: str | None = None,
    states: Iterable[str] = DEFAULT_STATES,
) -> list[InstanceConfiguration]:
    """Return full configurations for all instances, or one matched by ID or Name tag.

    When `resource` is given every state is searched, so stopped and terminated
    instances can still be inspected.
    """
    instance_ids: tuple[str, ...] = ()
    filters: tuple[dict[str, Any], ...] = ()
    if resource:
        states = INSTANCE_STATES
        if resource.startswith("i-"):
            instance_ids = (resource,)
        else:
            filters = ({"Name": "tag:Name", "Values": [resource]},)

    raws = describe_instances(region, states, instance_ids, filters)
    if resource and not raws:
        raise AwsApiError(f"no instance found matching '{resource}'", NOT_FOUND_HINTS)
    return build_configurations(raws, region)


def build_configurations(raws: list[dict[str, Any]], region: str | None = None) -> list[InstanceConfiguration]:
    """Turn describe_instances records into full configurations (instance types and volumes added)."""
    client = aws_auth.get_client("ec2", region=region)
    type_specs = _type_specs(client, {raw["InstanceType"] for raw in raws})
    volume_details = _volume_details(client, {
        mapping["Ebs"]["VolumeId"]
        for raw in raws
        for mapping in raw.get("BlockDeviceMappings", [])
        if "Ebs" in mapping
    })

    configurations = [_to_configuration(raw, type_specs, volume_details) for raw in raws]
    return sorted(configurations, key=lambda config: sort_key(config.instance))


def _yes_no(value: bool | None) -> str:
    return "" if value is None else ("yes" if value else "no")


def _launched(launch_time: datetime | None) -> str:
    """Return e.g. '2026-10-01 08:12 UTC (39 days ago)'."""
    if not launch_time:
        return ""
    days = (datetime.now(timezone.utc) - launch_time).days
    age = "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago"
    return f"{launch_time.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC ({age})"


def _volume_lines(volume: VolumeSpec) -> tuple[str, str]:
    """Return two lines: '/dev/xvda · vol-0abc' and '30 GiB gp3 · 3000 IOPS · encrypted'."""
    parts = [f"{volume.size_gib} GiB {volume.volume_type}".strip() if volume.size_gib else volume.volume_type]
    if volume.iops:
        parts.append(f"{volume.iops} IOPS")
    if volume.throughput:
        parts.append(f"{volume.throughput} MiB/s")
    if volume.encrypted is not None:
        parts.append("encrypted" if volume.encrypted else "not encrypted")
    if not volume.delete_on_termination:
        parts.append("kept after termination")
    details = " · ".join(part for part in parts if part)
    return f"{volume.device} · {volume.volume_id}", f"  {details}"


def print_configuration(config: InstanceConfiguration) -> None:
    """Print one instance's configuration, grouped into sections."""
    instance, spec = config.instance, config.type_spec
    indent = FIELD_INDENT

    print_instance_heading(instance)

    section("Overview")
    field("Instance ID", instance.instance_id, indent=indent)
    field("Status", instance.state, STATE_STYLES.get(instance.state, ""), indent=indent)
    field("Launched", _launched(config.launch_time), indent=indent)
    field_list("Tags", (f"{key} = {value}" for key, value in config.tags), indent=indent)

    section("Compute")
    field("Instance type", config.instance_type, "bold", indent=indent)
    field("vCPUs", spec.vcpus if spec else "", indent=indent)
    field("Memory", f"{spec.memory_gib:g} GiB" if spec else "", indent=indent)
    field("Network", spec.network if spec else "", indent=indent)
    field("Burstable", _yes_no(spec.burstable) if spec else "", indent=indent)
    field("Current gen", _yes_no(spec.current_generation) if spec else "", indent=indent)
    field("Architecture", config.architecture, indent=indent)
    field("Platform", config.platform, indent=indent)
    field("Lifecycle", config.lifecycle, indent=indent)
    field("Tenancy", config.tenancy, indent=indent)

    section("Network")
    field("Zone", config.availability_zone, indent=indent)
    field("VPC", instance.vpc_id, indent=indent)
    field("Subnet", config.subnet_id, indent=indent)
    field("Public IP", instance.public_ip, indent=indent)
    field("Private IP", instance.private_ip, indent=indent)
    field_list("Security groups", config.security_groups, indent=indent)

    section("Storage")
    field("Root device", config.root_device, indent=indent)
    field("EBS optimized", _yes_no(config.ebs_optimized), indent=indent)
    field_list(
        "Volumes",
        (line for volume in config.volumes for line in _volume_lines(volume)),
        indent=indent,
    )

    section("Access & monitoring")
    field("AMI", config.ami_id, indent=indent)
    field("Key pair", config.key_name, indent=indent)
    field("IAM profile", config.iam_profile, indent=indent)
    field("Monitoring", "detailed" if config.detailed_monitoring else "basic", indent=indent)


def print_configurations(configs: list[InstanceConfiguration], region: str) -> None:
    """Print a header line, then the configuration of each instance."""
    print_header(
        "EC2 configuration",
        region,
        len(configs),
        state_summary(config.instance for config in configs),
    )

    if not configs:
        console.print("  [dim]No instances match the selected states.[/]")
        console.print()
        return

    for config in configs:
        print_configuration(config)
        console.print()