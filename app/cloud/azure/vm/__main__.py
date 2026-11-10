"""Azure VM commands for the subscription configured in .env.

Usage:
    python -m app.cloud.azure.vm                             # list VMs
    python -m app.cloud.azure.vm list --location eastus      # VMs of one location
    python -m app.cloud.azure.vm config                      # specs of every VM
    python -m app.cloud.azure.vm config -r web-01            # one VM by name, vmId or resource ID
    python -m app.cloud.azure.vm run                         # analysis, month to date (production)
    python -m app.cloud.azure.vm run 7d                      # analysis, rolling 7 days (testing)
    python -m app.cloud.azure.vm evidence                    # step-by-step pipeline: AI evidence JSON
    python -m app.cloud.azure.vm validate evidence.json      # check an evidence file against the schema
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.auth.common import AuthError, hint, status
from app.cloud.azure.vm import pipeline
from app.cloud.azure.vm.analysis import print_analysis, run_analysis
from app.cloud.azure.vm.inventory import get_configurations, list_vms, print_configurations, print_vms
from app.cloud.windows import ANALYSIS_WINDOWS, DEFAULT_WINDOW

DEFAULT_COMMAND = "list"


def _scope(args: argparse.Namespace) -> str:
    return args.location or "all locations"


def _run_list(args: argparse.Namespace) -> None:
    print_vms(list_vms(args.location), _scope(args))


def _run_configuration(args: argparse.Namespace) -> None:
    print_configurations(get_configurations(args.location, args.resource), _scope(args))


def _run_analysis(args: argparse.Namespace) -> None:
    print_analysis(run_analysis(args.window or DEFAULT_WINDOW, args.location))


def _run_evidence(args: argparse.Namespace) -> int:
    return pipeline.main(args)


def _run_validate(args: argparse.Namespace) -> int:
    return pipeline.validate_file(args.file)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read the command and its options; with no command, 'list' is used."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = [DEFAULT_COMMAND, *argv]

    location_option = argparse.ArgumentParser(add_help=False)
    location_option.add_argument("--location", help="only this Azure location, e.g. eastus (default: every location)")

    parser = argparse.ArgumentParser(
        prog="python -m app.cloud.azure.vm",
        description="Azure VM discovery and analysis for the subscription configured in .env.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    commands.add_parser("list", parents=[location_option],
                        help="list VMs with status, location and resource group (default)").set_defaults(run=_run_list)

    config_cmd = commands.add_parser("configuration", aliases=["config"], parents=[location_option],
                                     help="show full configuration: size, CPU, memory, OS, placement, disks, tags")
    config_cmd.add_argument("-r", "--resource", metavar="NAME_OR_ID", help="one VM by name, vmId or resource ID")
    config_cmd.set_defaults(run=_run_configuration)

    run_cmd = commands.add_parser("run", aliases=["analyze"], parents=[location_option],
                                  help="run an analysis: collect configuration and hourly CPU/memory, store them, "
                                       "report changes and readiness")
    run_cmd.add_argument("window", nargs="?", choices=list(ANALYSIS_WINDOWS), metavar="WINDOW",
                         help=f"analysis window: {', '.join(ANALYSIS_WINDOWS)} (default: {DEFAULT_WINDOW})")
    run_cmd.set_defaults(run=_run_analysis)

    evidence_cmd = commands.add_parser("evidence", aliases=["prepare"],
                                       help="step-by-step pipeline: analysis, then validated JSON evidence for AI rightsizing")
    pipeline.add_arguments(evidence_cmd)
    evidence_cmd.set_defaults(run=_run_evidence)

    validate_cmd = commands.add_parser("validate", help="check an evidence JSON file against the schema")
    validate_cmd.add_argument("file", type=Path, help="evidence JSON file")
    validate_cmd.set_defaults(run=_run_validate)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit code 0 on success, 1 on failure."""
    args = parse_args(argv)
    try:
        return args.run(args) or 0
    except AuthError as exc:
        status(False, f"Azure VM {args.command} failed: {exc}")
        hint(exc.hints)
        return 1


raise SystemExit(main())
