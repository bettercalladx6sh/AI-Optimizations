"""EC2 commands for the AWS account configured in .env.

Usage:
    python -m app.cloud.aws.ec2                              # list instances
    python -m app.cloud.aws.ec2 list --state running         # list running only
    python -m app.cloud.aws.ec2 config                       # specs of all instances
    python -m app.cloud.aws.ec2 config -r i-0abc123          # one instance by ID or Name tag
    python -m app.cloud.aws.ec2 run                          # analysis, 30d window (production)
    python -m app.cloud.aws.ec2 run 90d                      # analysis, 90d window (production)
    python -m app.cloud.aws.ec2 run 1h                       # analysis, 1h window (testing)
    python -m app.cloud.aws.ec2 history -r i-0abc123         # stored changes of one instance
    python -m app.cloud.aws.ec2 run 7d --region eu-west-1    # another region
"""

from __future__ import annotations

import argparse
import sys

from app.auth import aws_auth
from app.auth.common import AuthError, hint, status
from app.cloud.aws.ec2.analysis import (
    ANALYSIS_WINDOWS,
    DEFAULT_WINDOW,
    get_history,
    print_analysis,
    print_history,
    run_analysis,
)
from app.cloud.aws.ec2.configuration import get_configurations, print_configurations
from app.cloud.aws.ec2.instances import DEFAULT_STATES, INSTANCE_STATES, list_instances, print_instances

DEFAULT_COMMAND = "list"
DEFAULT_HISTORY_LIMIT = 20


def _run_list(args: argparse.Namespace, region: str) -> None:
    print_instances(list_instances(region, args.state or DEFAULT_STATES), region)


def _run_configuration(args: argparse.Namespace, region: str) -> None:
    configs = get_configurations(region, args.resource, args.state or DEFAULT_STATES)
    print_configurations(configs, region)


def _run_analysis(args: argparse.Namespace, region: str) -> None:
    print_analysis(run_analysis(args.window, region))


def _run_history(args: argparse.Namespace, region: str) -> None:
    print_history(get_history(region, args.resource, args.limit), region)


def _positive(value: str) -> int:
    """argparse type: a whole number above zero."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _window_help() -> str:
    by_purpose: dict[str, list[str]] = {}
    for window in ANALYSIS_WINDOWS.values():
        by_purpose.setdefault(window.purpose, []).append(window.key)
    listed = "; ".join(f"{purpose}: {', '.join(keys)}" for purpose, keys in by_purpose.items())
    return f"analysis window ({listed}; default: {DEFAULT_WINDOW})"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read the command and its options; with no command, 'list' is used."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = [DEFAULT_COMMAND, *argv]

    region_option = argparse.ArgumentParser(add_help=False)
    region_option.add_argument(
        "--region",
        help="region to scan (default: AWS_DEFAULT_REGION from .env)",
    )

    state_option = argparse.ArgumentParser(add_help=False)
    state_option.add_argument(
        "--state",
        action="append",
        choices=INSTANCE_STATES,
        help="only include this state; repeat for several (default: all except terminated)",
    )

    parser = argparse.ArgumentParser(
        prog="python -m app.cloud.aws.ec2",
        description="EC2 discovery and analysis for the AWS account configured in .env.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    list_cmd = commands.add_parser(
        "list",
        parents=[region_option, state_option],
        help="list instances with status, VPC and IPs (default)",
    )
    list_cmd.set_defaults(run=_run_list)

    config_cmd = commands.add_parser(
        "configuration",
        aliases=["config"],
        parents=[region_option, state_option],
        help="show full configuration: type, CPU, memory, network, storage, tags",
    )
    config_cmd.add_argument(
        "-r", "--resource",
        metavar="ID_OR_NAME",
        help="one instance by ID (i-...) or Name tag; searches every state",
    )
    config_cmd.set_defaults(run=_run_configuration)

    run_cmd = commands.add_parser(
        "run",
        aliases=["analyze"],
        parents=[region_option],
        help="run an analysis: store/compare configuration, report changes and readiness",
    )
    run_cmd.add_argument(
        "window",
        nargs="?",
        default=DEFAULT_WINDOW,
        choices=list(ANALYSIS_WINDOWS),
        metavar="WINDOW",
        help=_window_help(),
    )
    run_cmd.set_defaults(run=_run_analysis)

    history_cmd = commands.add_parser(
        "history",
        parents=[region_option],
        help="show stored configuration changes, newest first",
    )
    history_cmd.add_argument(
        "-r", "--resource",
        metavar="ID_OR_NAME",
        help="only changes of one instance, by ID (i-...) or Name tag",
    )
    history_cmd.add_argument(
        "-n", "--limit",
        type=_positive,
        default=DEFAULT_HISTORY_LIMIT,
        metavar="COUNT",
        help=f"how many changes to show (default: {DEFAULT_HISTORY_LIMIT})",
    )
    history_cmd.set_defaults(run=_run_history)

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