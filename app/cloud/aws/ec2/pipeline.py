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
    python -m app.cloud.aws.ec2.pipeline                  # 30d (month to date)
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
from app.cloud.aws.ec2 import analysis
from app.cloud.aws.ec2.evidence import SCHEMA_VERSION, build_evidence, validate_evidence
from app.cloud.aws.ec2.windows import ANALYSIS_WINDOWS, DEFAULT_WINDOW
from app.cloud.display import plural
from app.cloud.files import PROJECT_ROOT, display_path, write_json
from app.cloud.schema import EvidenceValidationError, ValidationIssue

STEPS = (*analysis.STEPS, "Prepare evidence", "Validate evidence schema", "Update evidence file")
EVIDENCE_DIR = PROJECT_ROOT / "evidence"
MAX_ISSUES_SHOWN = 25


@dataclass(frozen=True)
class PipelineResult:
    """What the pipeline produced."""

    result: analysis.AnalysisResult
    evidence: dict[str, Any]
    path: Path | None


class StepPrinter:
    """Prints '✔ 3/9 Title · detail' as each step completes, and the failed step on error."""

    def __init__(self, titles: tuple[str, ...]) -> None:
        self.titles = titles
        self.done = 0
        self.width = max(len(title) for title in titles)
        self.digits = len(str(len(titles)))

    def __call__(self, title: str, detail: str) -> None:
        self.done += 1
        console.print(
            f"[bold green]✔[/] [dim]{self.done:>{self.digits}}/{len(self.titles)}[/] {title:<{self.width}}  [dim]{detail}[/]"
        )

    def failed(self, message: str, hints: tuple[str, ...] = ()) -> None:
        title = self.titles[min(self.done, len(self.titles) - 1)]
        console.print(f"[bold red]✘[/] [dim]{self.done + 1:>{self.digits}}/{len(self.titles)}[/] {title:<{self.width}}  [red]{message}[/]")
        hint(hints)


def default_path(document: dict[str, Any]) -> Path:
    """evidence/aws/ec2/<account>/<region>/<window>.json under the project root, e.g. .../30d.json."""
    source = document["source"]
    window = document["analysis_window"]["key"]
    return EVIDENCE_DIR / "aws" / "ec2" / source["account_id"] / source["region"] / f"{window}.json"


def print_issues(issues: list[ValidationIssue]) -> None:
    for issue in issues[:MAX_ISSUES_SHOWN]:
        console.print(f"    [red]•[/] {issue}", highlight=False)
    if len(issues) > MAX_ISSUES_SHOWN:
        console.print(f"    [dim]… and {len(issues) - MAX_ISSUES_SHOWN} more[/]")


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
    size = len(json.dumps(evidence))
    issue_count = sum(len(instance["data_quality"]["issues"]) for instance in evidence["instances"])
    steps(STEPS[6], f"{plural(len(evidence['instances']), 'instance')} · "
                    f"{plural(issue_count, 'data-quality issue')} · {size / 1024:.1f} KB")

    issues = validate_evidence(evidence)
    if issues:
        raise EvidenceValidationError(
            f"evidence failed validation with {plural(len(issues), 'issue')}; no file was written",
            [str(issue) for issue in issues[:MAX_ISSUES_SHOWN]],
        )
    steps(STEPS[7], f"schema {SCHEMA_VERSION} · valid")

    path = None
    if write_file:
        path = output or default_path(evidence)
        updated = write_json(evidence, path)
        steps(STEPS[8], f"{'updated' if updated else 'created'} {display_path(path)}")
    else:
        steps(STEPS[8], "skipped (--no-file) · evidence kept in memory for this run")

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
    console.print(f"[bold]EC2 evidence pipeline[/] [dim]·[/] {window.key} · {window.purpose} · {window.description}")
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
    """Validate an evidence JSON file; exit code 0 when valid, 1 otherwise."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        status(False, f"{path}: file not found")
        return 1
    except json.JSONDecodeError as exc:
        status(False, f"{path}: not valid JSON ({exc})")
        return 1
    try:
        issues = validate_evidence(document)
    except AuthError as exc:
        status(False, str(exc))
        return 1
    except (KeyError, TypeError, ValueError) as exc:
        status(False, f"{path}: unexpected structure ({exc})")
        return 1
    if issues:
        status(False, f"{path}: {plural(len(issues), 'issue')} against schema {SCHEMA_VERSION}")
        print_issues(issues)
        return 1
    status(True, f"{path}: valid evidence (schema {SCHEMA_VERSION}, {plural(len(document['instances']), 'instance')})")
    return 0


if __name__ == "__main__":
    cli = argparse.ArgumentParser(
        prog="python -m app.cloud.aws.ec2.pipeline",
        description="Prepare validated EC2 rightsizing evidence step by step.",
    )
    add_arguments(cli)
    sys.exit(main(cli.parse_args()))