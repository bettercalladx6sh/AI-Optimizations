"""EC2 instance discovery: read-only listing of instances in one AWS region."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from rich.markup import escape

from app.auth import aws_auth
from app.auth.common import console
from app.cloud.aws.errors import aws_errors
from app.cloud.display import field, print_header

INSTANCE_STATES = ("pending", "running", "shutting-down", "stopping", "stopped", "terminated")
DEFAULT_STATES = tuple(state for state in INSTANCE_STATES if state != "terminated")

STATE_STYLES = {
    "running": "green",
    "stopped": "red",
    "pending": "yellow",
    "stopping": "yellow",
    "shutting-down": "yellow",
    "terminated": "dim",
}

INSTANCE_ID_HINTS = {
    "InvalidInstanceID.Malformed": ("Instance IDs look like i-0123456789abcdef0",),
    "InvalidInstanceID.NotFound": ("Check the instance ID and --region",),
}


@dataclass(frozen=True)
class Ec2Instance:
    """The EC2 instance fields this app works with."""

    instance_id: str
    name: str
    state: str
    vpc_id: str
    public_ip: str
    private_ip: str


def tag_value(tags: Iterable[Mapping[str, str]], key: str) -> str:
    """Return the value of one resource tag, or an empty string."""
    return next((tag["Value"] for tag in tags if tag.get("Key") == key), "")


def to_instance(raw: Mapping[str, Any]) -> Ec2Instance:
    """Convert one describe_instances record into an Ec2Instance."""
    return Ec2Instance(
        instance_id=raw["InstanceId"],
        name=tag_value(raw.get("Tags", []), "Name"),
        state=raw["State"]["Name"],
        vpc_id=raw.get("VpcId", ""),
        public_ip=raw.get("PublicIpAddress", ""),
        private_ip=raw.get("PrivateIpAddress", ""),
    )


def sort_key(instance: Ec2Instance) -> tuple[bool, bool, str, str]:
    """Running first; within each group, named by name, unnamed last."""
    return (instance.state != "running", not instance.name, instance.name.lower(), instance.instance_id)


def describe_instances(
    region: str | None = None,
    states: Iterable[str] = DEFAULT_STATES,
    instance_ids: Iterable[str] = (),
    filters: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Return raw describe_instances records (all pages) matching the given filters."""
    client = aws_auth.get_client("ec2", region=region)
    request: dict[str, Any] = {
        "Filters": [{"Name": "instance-state-name", "Values": list(states)}, *filters],
    }
    if ids := list(instance_ids):
        request["InstanceIds"] = ids

    with aws_errors("ec2:DescribeInstances", INSTANCE_ID_HINTS):
        return [
            raw
            for page in client.get_paginator("describe_instances").paginate(**request)
            for reservation in page["Reservations"]
            for raw in reservation["Instances"]
        ]


def list_instances(
    region: str | None = None,
    states: Iterable[str] = DEFAULT_STATES,
) -> list[Ec2Instance]:
    """Return every instance in the region whose state is in `states`, sorted."""
    return sorted((to_instance(raw) for raw in describe_instances(region, states)), key=sort_key)


def print_instance_heading(instance: Ec2Instance) -> None:
    """Print the coloured state dot and the instance name."""
    style = STATE_STYLES.get(instance.state, "white")
    name = escape(instance.name) if instance.name else "[dim](no name)[/]"
    console.print(f"[{style}]●[/] [bold]{name}[/]")


def state_summary(instances: Iterable[Ec2Instance]) -> str:
    """Return e.g. '2 running, 1 stopped'."""
    counts = Counter(instance.state for instance in instances)
    return ", ".join(f"{count} {state}" for state, count in counts.most_common())


def print_instances(instances: list[Ec2Instance], region: str) -> None:
    """Print instances as readable blocks: a header line, then one block per instance."""
    print_header("EC2 instances", region, len(instances), state_summary(instances))

    if not instances:
        console.print("  [dim]No instances match the selected states.[/]")
        console.print()
        return

    for instance in instances:
        print_instance_heading(instance)
        field("Instance ID", instance.instance_id)
        field("Status", instance.state, STATE_STYLES.get(instance.state, ""))
        field("VPC", instance.vpc_id)
        field("Public IP", instance.public_ip)
        field("Private IP", instance.private_ip)
        console.print()