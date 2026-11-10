"""EC2 evidence pipeline: the entry point that prepares rightsizing evidence step by step.

    1. Connect to AWS
    2. Find the instances
    3. Get instance details and configurations
    4. Check for configuration changes
    5. Collect CloudWatch metrics (missing hours only)
    6. Store data and calculate cumulative averages
    7. Prepare evidence
    8. Validate evidence schema
    9. Write the evidence JSON file

Steps 1-6 are the analysis run (analysis.run_analysis); 7-9 build, validate and write the JSON
evidence that AI evaluation will use for rightsizing decisions. Evidence is built in memory for
each run and is not stored in the database. It is written to one JSON file per account, region
and window (evidence/aws/ec2/<account>/<region>/<window>.json), which every new run of that
window replaces atomically, and only after the new evidence passed validation.

Usage:
    python -m app.cloud.aws.ec2.pipeline                  # default window from app/config.py
    python -m app.cloud.aws.ec2.pipeline 7d -o ev.json    # testing window, custom file
    python -m app.cloud.aws.ec2 evidence                  # same pipeline, shorthand
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.auth import aws_auth
from app.auth.common import AuthError, console, hint, status
from app.cloud import evidence_output
from app.cloud.aws.ec2 import analysis
from app.cloud.aws.ec2.evidence import SCHEMA_VERSION, build_evidence, validate_evidence
from app.cloud.display import StepPrinter
from app.cloud.evidence_output import EVIDENCE_DIR, MAX_ISSUES_SHOWN, OUTPUT_STEPS, print_issues
from app.cloud.windows import ANALYSIS_WINDOWS, DEFAULT_WINDOW

STEPS = (*analysis.STEPS, *OUTPUT_STEPS)

__all__ = ["EVIDENCE_DIR", "MAX_ISSUES_SHOWN", "STEPS", "PipelineResult", "StepPrinter", "add_arguments",
           "default_path", "main", "print_issues", "run_pipeline", "validate_file"]


@dataclass(frozen=True)
class PipelineResult:
    """What the pipeline produced."""

    result: analysis.AnalysisResult
    evidence: dict[str, Any]
    path: Path | None


def default_path(document: dict[str, Any]) -> Path:
    """evidence/aws/ec2/<account>/<region>/<window>.json under the project root, e.g. .../30d.json."""
    return evidence_output.evidence_path(document)


def run_pipeline(
    window_key: str = DEFAULT_WINDOW,
    region: str | None = None,
    output: Path | None = None,
    write_file: bool = True,
    steps: StepPrinter | None = None,
) -> PipelineResult:
    """Run all nine steps and return the analysis result and the validated evidence."""
    steps = steps or StepPrinter(STEPS)
    result = analysis.run_analysis(window_key, region, on_step=steps)
    evidence = build_evidence(result)
    path = evidence_output.publish(evidence, validate_evidence, SCHEMA_VERSION, steps, output, write_file)
    return PipelineResult(result, evidence, path)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Options of the pipeline, shared by this module and `python -m app.cloud.aws.ec2 evidence`."""
    parser.add_argument(
        "window",
        nargs="?",
        default=DEFAULT_WINDOW,
        choices=list(ANALYSIS_WINDOWS),
        metavar="WINDOW",
        help=f"analysis window: {', '.join(ANALYSIS_WINDOWS)} (default: {DEFAULT_WINDOW})",
    )
    parser.add_argument("--region", help="region to analyse (default: AWS_DEFAULT_REGION from .env)")
    parser.add_argument(
        "-o", "--output",
        type=Path,
        metavar="FILE",
        help="evidence JSON file to create or update (default: evidence/aws/ec2/<account>/<region>/<window>.json)",
    )
    parser.add_argument("--no-file", action="store_true", help="do not write the JSON file (use with -p)")
    parser.add_argument("-p", "--print", dest="print_json", action="store_true", help="also print the evidence JSON")


def main(args: argparse.Namespace) -> int:
    """Run the pipeline with parsed arguments; exit code 0 on success, 1 on failure."""
    window = ANALYSIS_WINDOWS[args.window]
    console.print()
    console.print(f"[bold]EC2 evidence pipeline[/] [dim]·[/] {window.label(*window.bounds())}")
    console.print()

    steps = StepPrinter(STEPS)
    try:
        region = args.region or aws_auth.get_session().region_name
        outcome = run_pipeline(args.window, region, args.output, not args.no_file, steps)
    except AuthError as exc:
        steps.failed(str(exc), exc.hints)
        return 1

    console.print()
    summary = outcome.evidence["summary"]
    status(True, f"Evidence {outcome.evidence['evidence_id']} is ready for AI evaluation")
    hint((
        f"{summary['ready_for_evaluation']} of {summary['instance_count']} instances ready for evaluation · "
        f"{summary['instances_with_issues']} with data-quality issues",
        "Full analysis output: python -m app.cloud.aws.ec2 run " + args.window,
    ))
    if args.print_json:
        console.print_json(json.dumps(outcome.evidence))
    return 0


def validate_file(path: Path) -> int:
    """Validate an EC2 evidence JSON file; exit code 0 when valid, 1 otherwise."""
    return evidence_output.validate_file(path, validate_evidence, SCHEMA_VERSION)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(
        prog="python -m app.cloud.aws.ec2.pipeline",
        description="Prepare validated EC2 rightsizing evidence step by step.",
    )
    add_arguments(cli)
    sys.exit(main(cli.parse_args()))