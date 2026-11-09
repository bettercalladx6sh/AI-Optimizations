"""EC2 discovery commands for the AWS account configured in .env.

Usage:
    python -m app.cloud.aws.ec2                                   # list instances
    python -m app.cloud.aws.ec2 list --state running              # list running only
    python -m app.cloud.aws.ec2 configuration                     # specs of all instances
    python -m app.cloud.aws.ec2 configuration -r i-0abc123        # one instance by ID
    python -m app.cloud.aws.ec2 config -r web-server-01           # short form, by Name tag
    python -m app.cloud.aws.ec2 configuration --region eu-west-1  # another region
"""

from __future__ import annotations

import argparse
import sys

from app.auth import aws_auth
from app.auth.common import AuthError, hint, status
from app.cloud.aws.ec2.configuration import get_configurations, print_configurations
from app.cloud.aws.ec2.instances import DEFAULT_STATES, INSTANCE_STATES, list_instances, print_instances

DEFAULT_COMMAND = "list"


def _run_list(args: argparse.Namespace, region: str) -> None:
    print_instances(list_instances(region, args.state or DEFAULT_STATES), region)


def _run_configuration(args: argparse.Namespace, region: str) -> None:
    configs = get_configurations(region, args.resource, args.state or DEFAULT_STATES)
    print_configurations(configs, region)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read the command and its options; with no command, 'list' is used."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = [DEFAULT_COMMAND, *argv]

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--region",
        help="region to scan (default: AWS_DEFAULT_REGION from .env)",
    )
    shared.add_argument(
        "--state",
        action="append",
        choices=INSTANCE_STATES,
        help="only include this state; repeat for several (default: all except terminated)",
    )

    parser = argparse.ArgumentParser(
        prog="python -m app.cloud.aws.ec2",
        description="EC2 discovery for the AWS account configured in .env (read-only).",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    list_cmd = commands.add_parser(
        "list", parents=[shared], help="list instances with status, VPC and IPs (default)"
    )
    list_cmd.set_defaults(run=_run_list)

    config_cmd = commands.add_parser(
        "configuration",
        aliases=["config"],
        parents=[shared],
        help="show full configuration: type, CPU, memory, network, storage, tags",
    )
    config_cmd.add_argument(
        "-r", "--resource",
        metavar="ID_OR_NAME",
        help="one instance by ID (i-...) or Name tag; searches every state",
    )
    config_cmd.set_defaults(run=_run_configuration)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit code 0 on success, 1 on failure."""
    args = parse_args(argv)

    try:
        region = args.region or aws_auth.get_session().region_name
        args.run(args, region)
    except AuthError as exc:
        status(False, f"EC2 {args.command} failed: {exc}")
        hint(exc.hints)
        return 1
    return 0


raise SystemExit(main())