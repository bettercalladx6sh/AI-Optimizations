"""Full analysis of one cloud: the AI rightsizing pipeline, then the OpenSearch super index
query and filtering, each writing its JSON report.

    python -m app.analyze --analyze azure

    1/2 AI rightsizing   python -m app.reasoning --cloud azure
                         Azure VM metrics and evidence, pay-as-you-go pricing, OpenAI reasoning
                         -> recommendations/azure/vm/<subscription>/<location|all>/<window>.json
    2/2 OpenSearch       python -m app.server.opensearch.azure.vm_super_index
                         Azure VM documents of the active tenants for the year, matched to the
                         completed ingestion jobs, newest job first
                         -> opensearch_results/azure_opensearch.json

Both parts are the existing commands, run in order with the options below passed on, so
their output, files and checks are exactly the same as running them one by one. Each JSON
file is replaced in place on every run. A part that fails does not stop the other one; the
exit code is 1 when any part failed. Read-only on OpenSearch and on the company database.

Usage:
    python -m app.analyze --analyze azure                    # default window, every location
    python -m app.analyze --analyze azure 7d --location eastus
    python -m app.analyze --analyze azure --no-ai            # rule-based decisions, no OpenAI call
    python -m app.analyze --analyze azure --only opensearch  # one part only (ai or opensearch)
    python -m app.analyze --analyze azure --year 2025 --show 0 -q
    python -m app.analyze --analyze aws --region us-east-1   # the same for AWS EC2
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass

from rich.markup import escape

from app.auth.common import console, status
from app.cloud.windows import ANALYSIS_WINDOWS, DEFAULT_WINDOW
from app.reasoning import __main__ as reasoning_cli
from app.server.mysql.data_ingestion_audit import DEFAULT_YEAR
from app.server.opensearch.aws.ec2 import ec2_super_index
from app.server.opensearch.azure import vm_super_index
from app.server.opensearch.super_index import DEFAULT_SHOWN

PARTS = ("ai", "opensearch")


@dataclass(frozen=True)
class Analysis:
    """One cloud: its title, its app.reasoning --cloud key, its scope option and its super index command."""

    title: str
    cloud: str
    scope_option: str
    opensearch_command: str
    opensearch: Callable[[list[str] | None], int]


ANALYSES = {
    "aws": Analysis("AWS EC2", "aws", "--region",
                    "python -m app.server.opensearch.aws.ec2.ec2_super_index", ec2_super_index.main),
    "azure": Analysis("Azure VM", "azure", "--location",
                      "python -m app.server.opensearch.azure.vm_super_index", vm_super_index.main),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.analyze",
        description="Run the AI rightsizing pipeline and the OpenSearch super index query of one cloud, "
                    "writing both JSON reports.",
    )
    parser.add_argument("--analyze", required=True, choices=list(ANALYSES), metavar="CLOUD",
                        help=f"cloud to analyse: {', '.join(ANALYSES)}")
    parser.add_argument("window", nargs="?", default=DEFAULT_WINDOW, choices=list(ANALYSIS_WINDOWS), metavar="WINDOW",
                        help=f"AI analysis window: {', '.join(ANALYSIS_WINDOWS)} (default: {DEFAULT_WINDOW})")
    parser.add_argument("--location", help="Azure location for the AI part, e.g. eastus (default: every location)")
    parser.add_argument("--region", help="AWS region for the AI part (default: AWS_DEFAULT_REGION from .env)")
    parser.add_argument("--no-ai", action="store_true", help="rule-based decisions only; no OpenAI call")
    parser.add_argument("--only", choices=PARTS, help="run one part only (default: both)")
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR,
                        help=f"OpenSearch: year of audit_date and document timestamp (default: {DEFAULT_YEAR})")
    parser.add_argument("--show", type=int, default=DEFAULT_SHOWN, metavar="COUNT",
                        help=f"OpenSearch: documents listed per job, 0 for all (default: {DEFAULT_SHOWN})")
    parser.add_argument("-q", "--quiet", action="store_true", help="OpenSearch: no progress lines while fetching")
    args = parser.parse_args(argv)
    if args.location and args.analyze != "azure":
        parser.error("--location is for --analyze azure; use --region for AWS")
    if args.region and args.analyze != "aws":
        parser.error("--region is for --analyze aws; use --location for Azure")
    if args.show < 0:
        parser.error("--show must be 0 or more")
    return args


def ai_argv(analysis: Analysis, args: argparse.Namespace) -> list[str]:
    """The app.reasoning arguments for this run."""
    argv = [args.window, "--cloud", analysis.cloud]
    scope = args.location or args.region
    if scope:
        argv += [analysis.scope_option, scope]
    if args.no_ai:
        argv.append("--no-ai")
    return argv


def opensearch_argv(args: argparse.Namespace) -> list[str]:
    """The super index command arguments for this run."""
    argv = ["--year", str(args.year), "--show", str(args.show)]
    if args.quiet:
        argv.append("-q")
    return argv


def _heading(number: int, total: int, title: str, command: str, argv: list[str]) -> None:
    console.print()
    console.print(f"[bold cyan]{number}/{total}[/] [bold]{escape(title)}[/] "
                  f"[dim]· {escape(' '.join([command, *argv]))}[/]", highlight=False)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit code 0 when every part succeeded, 1 otherwise."""
    args = parse_args(argv)
    analysis = ANALYSES[args.analyze]
    parts: list[tuple[str, str, list[str], Callable[[list[str] | None], int]]] = []
    if args.only in (None, "ai"):
        parts.append(("AI rightsizing", "python -m app.reasoning", ai_argv(analysis, args), reasoning_cli.main))
    if args.only in (None, "opensearch"):
        parts.append(("OpenSearch super index", analysis.opensearch_command, opensearch_argv(args), analysis.opensearch))

    console.print()
    console.print(f"[bold]{escape(analysis.title)} analysis[/] [dim]· {len(parts)} "
                  f"part{'s' if len(parts) != 1 else ''}[/]", highlight=False)
    results: list[tuple[str, bool]] = []
    for number, (title, command, part_argv, run) in enumerate(parts, 1):
        _heading(number, len(parts), title, command, part_argv)
        results.append((title, run(part_argv) == 0))

    console.print()
    for title, ok in results:
        status(ok, f"{title} {'completed' if ok else 'failed'}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())