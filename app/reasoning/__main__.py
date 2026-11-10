"""Cloudpositive AI Engine: EC2 and Azure VM rightsizing recommendations.

Usage:
    python -m app.reasoning                        # default window (app/config.py): EC2 pipeline, then AI
    python -m app.reasoning 7d                     # testing window
    python -m app.reasoning --cloud azure          # Azure VMs of the subscription (every location)
    python -m app.reasoning --cloud azure 7d --location eastus
    python -m app.reasoning --no-ai                # rule-based decisions only (no OpenAI call)
    python -m app.reasoning -e evidence/aws/ec2/<account>/<region>/30d.json   # reuse evidence (any cloud)
    python -m app.reasoning 1h -o rec.json         # custom output file
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.auth.common import AuthError, console, hint, status
from app.cloud.display import StepPrinter, plural
from app.cloud.files import display_path
from app.cloud.windows import ANALYSIS_WINDOWS, DEFAULT_WINDOW
from app.reasoning import engine, render
from app.reasoning.config import load_settings
from app.reasoning.providers import PROVIDERS, provider_for


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.reasoning",
        description="Cloudpositive AI Engine: evidence-based EC2 and Azure VM rightsizing recommendations.",
    )
    parser.add_argument("window", nargs="?", default=DEFAULT_WINDOW, choices=list(ANALYSIS_WINDOWS),
                        metavar="WINDOW", help=f"analysis window: {', '.join(ANALYSIS_WINDOWS)} (default: {DEFAULT_WINDOW})")
    parser.add_argument("--cloud", choices=list(PROVIDERS), default="aws",
                        help="cloud to analyse when the pipeline runs (default: aws; -e uses the evidence's cloud)")
    parser.add_argument("--region", help="AWS region to analyse (default: AWS_DEFAULT_REGION from .env)")
    parser.add_argument("--location", help="Azure location to analyse, e.g. eastus (default: every location)")
    parser.add_argument("-e", "--evidence", type=Path, metavar="FILE",
                        help="use an existing evidence JSON file instead of running the pipeline")
    parser.add_argument("--no-ai", action="store_true", help="rule-based decisions only; no OpenAI call")
    parser.add_argument("-o", "--output", type=Path, metavar="FILE",
                        help="recommendations JSON to create or update "
                             "(default: recommendations/<cloud>/<service>/<account>/<region>/<window>.json)")
    parser.add_argument("-p", "--print", dest="print_json", action="store_true", help="also print the JSON")
    args = parser.parse_args(argv)
    if args.region and args.cloud != "aws":
        parser.error("--region is for --cloud aws; use --location for Azure")
    if args.location and args.cloud != "azure":
        parser.error("--location is for --cloud azure; use --region for AWS")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    provider = PROVIDERS[args.cloud]
    steps = None
    try:
        settings = None if args.no_ai else load_settings()
        evidence = None
        if args.evidence:
            evidence = engine.load_evidence_file(args.evidence)
            provider = provider_for(evidence)
            label, scope = render.window_label(evidence["analysis_window"]), evidence["source"]["region"]
            steps = StepPrinter(("Load evidence file",) + engine.REASONING_STEPS)
        else:
            scope = args.region or args.location or provider.default_scope()
            window = ANALYSIS_WINDOWS[args.window]
            label = window.label(*window.bounds())
            steps = StepPrinter(provider.pipeline_steps + engine.REASONING_STEPS)
        render.print_title(label, scope or "all locations", provider.title)
        if evidence:
            steps("Load evidence file", f"{display_path(args.evidence)} · "
                                        f"{plural(len(evidence['instances']), 'instance')}")
        outcome = engine.run(args.window, scope, evidence, settings, args.output, steps, provider=provider)
    except AuthError as exc:
        if steps and steps.done:
            steps.failed(str(exc), exc.hints)
        else:
            status(False, str(exc))
            hint(exc.hints)
        return 1

    console.print()
    render.print_report(outcome.report, provider.price_kind)
    console.print()
    status(True, f"Recommendations written to {display_path(outcome.path)}")
    if args.print_json:
        console.print_json(json.dumps(outcome.report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
