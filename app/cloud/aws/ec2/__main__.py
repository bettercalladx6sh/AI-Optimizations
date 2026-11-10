"""EC2 commands for the AWS account configured in .env.

Usage:
    python -m app.cloud.aws.ec2                              # list instances
    python -m app.cloud.aws.ec2 list --state running         # list running only
    python -m app.cloud.aws.ec2 config                       # specs of all instances
    python -m app.cloud.aws.ec2 config -r i-0abc123          # one instance by ID or Name tag
    python -m app.cloud.aws.ec2 run                          # analysis, month to date (production)
    python -m app.cloud.aws.ec2 run 90d                      # analysis, last 3 calendar months to date
    python -m app.cloud.aws.ec2 run 1h                       # analysis, rolling 1h (testing)
    python -m app.cloud.aws.ec2 metrics                      # quick: month to date, all instances
    python -m app.cloud.aws.ec2 metrics -d yesterday         # quick: one day, every hour
    python -m app.cloud.aws.ec2 metrics -t "2026-11-10 06"   # quick: one hour
    python -m app.cloud.aws.ec2 metrics 7d -r api-01         # quick: one instance
    python -m app.cloud.aws.ec2 metrics -o                   # quick, stored data only (no CloudWatch)
    python -m app.cloud.aws.ec2 history -r i-0abc123         # stored changes of one instance
    python -m app.cloud.aws.ec2 evidence                     # step-by-step pipeline: AI evidence JSON
    python -m app.cloud.aws.ec2 validate evidence.json       # check an evidence file against the schema
    python -m app.cloud.aws.ec2 run 7d --region eu-west-1    # another region
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from app.auth import aws_auth
from app.auth.common import AuthError, hint, status, warning
from app.cloud.aws.ec2 import pipeline, utilization
from app.cloud.aws.ec2.analysis import get_history, print_analysis, print_history, run_analysis
from app.cloud.aws.ec2.configuration import get_configurations, print_configurations
from app.cloud.aws.ec2.instances import DEFAULT_STATES, INSTANCE_STATES, list_instances, print_instances
from app.cloud.windows import ANALYSIS_WINDOWS, DEFAULT_WINDOW
from app.cloud.display import print_header

DEFAULT_COMMAND = "list"
DEFAULT_HISTORY_LIMIT = 20


def _run_list(args: argparse.Namespace, region: str) -> None:
    print_instances(list_instances(region, args.state or DEFAULT_STATES), region)


def _run_configuration(args: argparse.Namespace, region: str) -> None:
    configs = get_configurations(region, args.resource, args.state or DEFAULT_STATES)
    print_configurations(configs, region)


def _run_analysis(args: argparse.Namespace, region: str) -> None:
    print_analysis(run_analysis(args.window or DEFAULT_WINDOW, region))


def _run_metrics(args: argparse.Namespace, region: str) -> None:
    window = ANALYSIS_WINDOWS[args.window or DEFAULT_WINDOW]
    selection = args.day or args.hour or utilization.window_selection(window)
    report = utilization.analyze(selection, region, args.resource, args.offline)
    print_header("EC2 utilization", region, report.instances, selection.label)
    if not report.series:
        warning("No stored instances match; run an analysis first: python -m app.cloud.aws.ec2 run")
        return
    utilization.print_report(report, hourly=args.hourly or selection.key in ("day", "hour"))


def _run_history(args: argparse.Namespace, region: str) -> None:
    print_history(get_history(region, args.resource, args.limit), region)


def _run_evidence(args: argparse.Namespace, region: str) -> int:
    args.region = region
    return pipeline.main(args)


def _run_validate(args: argparse.Namespace, region: str | None) -> int:
    return pipeline.validate_file(args.file)


def _positive(value: str) -> int:
    """argparse type: a whole number above zero."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _selection(parse: Callable[[str], utilization.Selection]) -> Callable[[str], utilization.Selection]:
    """argparse type for --day / --hour that reports bad values as usage errors."""
    def convert(value: str) -> utilization.Selection:
        try:
            return parse(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
    return convert


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

    window_option = argparse.ArgumentParser(add_help=False)
    window_option.add_argument(
        "window",
        nargs="?",
        choices=list(ANALYSIS_WINDOWS),
        metavar="WINDOW",
        help=_window_help(),
    )

    run_cmd = commands.add_parser(
        "run",
        aliases=["analyze"],
        parents=[region_option, window_option],
        help="run an analysis: collect configuration and hourly CPU/memory, store them, "
        "report changes and readiness",
    )
    run_cmd.set_defaults(run=_run_analysis)

    metrics_cmd = commands.add_parser(
        "metrics",
        aliases=["usage"],
        parents=[region_option, window_option],
        help="quick analysis: fill only missing hours, then show datapoints and CPU/memory",
    )
    period = metrics_cmd.add_mutually_exclusive_group()
    period.add_argument(
        "-d", "--day",
        type=_selection(utilization.day_selection),
        metavar="DAY",
        help="one UTC day: YYYY-MM-DD, today or yesterday (lists every hour)",
    )
    period.add_argument(
        "-t", "--hour",
        type=_selection(utilization.hour_selection),
        metavar="HOUR",
        help="one UTC hour: 'YYYY-MM-DD HH', or HH for today",
    )
    metrics_cmd.add_argument(
        "-r", "--resource",
        metavar="ID_OR_NAME",
        help="only one instance, by ID (i-...) or Name tag",
    )
    metrics_cmd.add_argument(
        "-H", "--hourly",
        action="store_true",
        help="also list every hourly average",
    )
    metrics_cmd.add_argument(
        "-o", "--offline",
        action="store_true",
        help="use stored data only; make no CloudWatch calls",
    )
    metrics_cmd.set_defaults(run=_run_metrics)

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

    evidence_cmd = commands.add_parser(
        "evidence",
        aliases=["prepare"],
        help="step-by-step pipeline: analysis, then validated JSON evidence for AI rightsizing",
    )
    pipeline.add_arguments(evidence_cmd)
    evidence_cmd.set_defaults(run=_run_evidence)

    validate_cmd = commands.add_parser("validate", help="check an evidence JSON file against the schema")
    validate_cmd.add_argument("file", type=Path, help="evidence JSON file")
    validate_cmd.set_defaults(run=_run_validate, needs_region=False)

    args = parser.parse_args(argv)
    if getattr(args, "window", None) and (getattr(args, "day", None) or getattr(args, "hour", None)):
        parser.error("give either a WINDOW or --day/--hour, not both")
    return args


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit code 0 on success, 1 on failure."""
    args = parse_args(argv)

    try:
        region = None
        if getattr(args, "needs_region", True):
            region = args.region or aws_auth.get_session().region_name
        return args.run(args, region) or 0
    except AuthError as exc:
        status(False, f"EC2 {args.command} failed: {exc}")
        hint(exc.hints)
        return 1


raise SystemExit(main())