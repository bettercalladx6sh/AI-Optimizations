"""List EC2 instances in the AWS account configured in .env.

Usage:
    python -m app.cloud.aws.ec2                                  # all except terminated
    python -m app.cloud.aws.ec2 --state running                  # running only
    python -m app.cloud.aws.ec2 --state running --state stopped  # several states
    python -m app.cloud.aws.ec2 --region eu-west-1               # another region
"""

from __future__ import annotations

import argparse

from app.auth import aws_auth
from app.auth.common import AuthError, hint, status
from app.cloud.aws.ec2.instances import (
    DEFAULT_STATES,
    INSTANCE_STATES,
    list_instances,
    print_instances,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read the command-line options."""
    parser = argparse.ArgumentParser(
        prog="python -m app.cloud.aws.ec2",
        description="List EC2 instances in the AWS account configured in .env.",
    )
    parser.add_argument(
        "--region",
        help="region to scan (default: AWS_DEFAULT_REGION from .env)",
    )
    parser.add_argument(
        "--state",
        action="append",
        choices=INSTANCE_STATES,
        help="only show this state; repeat for several (default: all except terminated)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit code 0 on success, 1 on failure."""
    args = parse_args(argv)

    try:
        region = args.region or aws_auth.get_session().region_name
        instances = list_instances(region=region, states=args.state or DEFAULT_STATES)
    except AuthError as exc:
        status(False, f"Listing EC2 instances failed: {exc}")
        hint(exc.hints)
        return 1

    print_instances(instances, region)
    return 0


raise SystemExit(main())