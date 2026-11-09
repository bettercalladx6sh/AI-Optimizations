"""EC2 instance discovery: read-only listing of instances in one AWS region."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from rich.markup import escape

from app.auth import aws_auth
from app.auth.common import AuthError, console

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
NOT_SET = "—"
LABEL_WIDTH = 13

PERMISSION_HINTS = ("Grant the IAM identity the ec2:DescribeInstances permission",)


class Ec2Error(AuthError):
    """Raised when EC2 cannot be queried (permissions, region or network)."""


@dataclass(frozen=True)
class Ec2Instance:
    """The EC2 instance fields this app works with."""

    instance_id: str
    name: str
    state: str
    vpc_id: str
    public_ip: str
    private_ip: str


def _tag(tags: Iterable[dict[str, str]], key: str) -> str:
    """Return the value of one resource tag, or an empty string."""
    return next((tag["Value"] for tag in tags if tag.get("Key") == key), "")


def _to_instance(raw: dict[str, Any]) -> Ec2Instance:
    """Convert one describe_instances record into an Ec2Instance."""
    return Ec2Instance(
        instance_id=raw["InstanceId"],
        name=_tag(raw.get("Tags", []), "Name"),
        state=raw["State"]["Name"],
        vpc_id=raw.get("VpcId", ""),
        public_ip=raw.get("PublicIpAddress", ""),
        private_ip=raw.get("PrivateIpAddress", ""),
    )


def list_instances(
    region: str | None = None,
    states: Iterable[str] = DEFAULT_STATES,
) -> list[Ec2Instance]:
    """Return every instance in the region whose state is in `states`.

    Running instances come first; within each group named instances are sorted
    by name and unnamed ones come last.
    """
    client = aws_auth.get_client("ec2", region=region)
    filters = [{"Name": "instance-state-name", "Values": list(states)}]

    try:
        instances = [
            _to_instance(raw)
            for page in client.get_paginator("describe_instances").paginate(Filters=filters)
            for reservation in page["Reservations"]
            for raw in reservation["Instances"]
        ]
    except ClientError as exc:
        error = exc.response["Error"]
        hints = PERMISSION_HINTS if error.get("Code") == "UnauthorizedOperation" else ()
        raise Ec2Error(error.get("Message", str(exc)), hints) from exc
    except BotoCoreError as exc:
        raise Ec2Error(str(exc)) from exc

    return sorted(
        instances,
        key=lambda i: (i.state != "running", not i.name, i.name.lower(), i.instance_id),
    )


def _field(label: str, value: str, style: str = "") -> None:
    """Print one indented 'label  value' line."""
    text = escape(value) if value else f"[dim]{NOT_SET}[/]"
    if style and value:
        text = f"[{style}]{text}[/]"
    console.print(f"  [dim]{label:<{LABEL_WIDTH}}[/]{text}")


def print_instances(instances: list[Ec2Instance], region: str) -> None:
    """Print instances as readable blocks: a header line, then one block per instance."""
    counts = Counter(instance.state for instance in instances)
    summary = ", ".join(f"{count} {state}" for state, count in counts.most_common())

    console.print()
    console.print(
        f"[bold]EC2 instances[/] [dim]·[/] {escape(region)} [dim]·[/] {len(instances)} found"
        + (f" [dim]({summary})[/]" if summary else "")
    )
    console.print()

    if not instances:
        console.print("  [dim]No instances match the selected states.[/]")
        console.print()
        return

    for instance in instances:
        style = STATE_STYLES.get(instance.state, "white")
        name = escape(instance.name) if instance.name else "[dim](no name)[/]"
        console.print(f"[{style}]●[/] [bold]{name}[/]")
        _field("Instance ID", instance.instance_id)
        _field("Status", instance.state, style)
        _field("VPC", instance.vpc_id)
        _field("Public IP", instance.public_ip)
        _field("Private IP", instance.private_ip)
        console.print()