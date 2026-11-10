"""Cloudpositive AI Engine: EC2 rightsizing recommendations.

Usage:
    python -m app.reasoning                        # 30d: evidence pipeline, then AI reasoning
    python -m app.reasoning 7d                     # testing window
    python -m app.reasoning --no-ai                # rule-based decisions only (no OpenAI call)
    python -m app.reasoning -e evidence/aws/ec2/<account>/<region>/30d.json   # reuse evidence
    python -m app.reasoning 1h -o rec.json         # custom output file
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.auth import aws_auth
from app.auth.common import AuthError, console, hint, status
from app.cloud.aws.ec2 import pipeline
from app.cloud.aws.ec2.windows import ANALYSIS_WINDOWS, DEFAULT_WINDOW
from app.cloud.display import plural
from app.cloud.files import display_path
from app.reasoning import engine, render
from app.reasoning.config import load_settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.reasoning",
        description="Cloudpositive AI Engine: evidence-based EC2 rightsizing recommendations.",
    )
    parser.add_argument("window", nargs="?", default=DEFAULT_WINDOW, choices=list(ANALYSIS_WINDOWS),
                        metavar="WINDOW", help=f"analysis window: {', '.join(ANALYSIS_WINDOWS)} (default: {DEFAULT_WINDOW})")
    parser.add_argument("--region", help="region to analyse (default: AWS_DEFAULT_REGION from .env)")
    parser.add_argument("-e", "--evidence", type=Path, metavar="FILE",
                        help="use an existing evidence JSON file instead of running the pipeline")
    parser.add_argument("--no-ai", action="store_true", help="rule-based decisions only; no OpenAI call")
    parser.add_argument("-o", "--output", type=Path, metavar="FILE",
                        help="recommendations JSON to create or update "
                             "(default: recommendations/aws/ec2/<account>/<region>/<window>.json)")
    parser.add_argument("-p", "--print", dest="print_json", action="store_true", help="also print the JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    titles = (("Load evidence file",) if args.evidence else pipeline.STEPS) + engine.REASONING_STEPS
    steps = pipeline.StepPrinter(titles)
    try:
        settings = None if args.no_ai else load_settings()
        evidence = None
        if args.evidence:
            evidence = engine.load_evidence_file(args.evidence)
            window, region = evidence["analysis_window"], evidence["source"]["region"]
        else:
            region = args.region or aws_auth.get_session().region_name
            window = {"key": args.window, **{k: getattr(ANALYSIS_WINDOWS[args.window], k)
                                               for k in ("purpose", "description")}}
        render.print_title(window, region)
        if evidence:
            steps("Load evidence file", f"{display_path(args.evidence)} · "
                                        f"{plural(len(evidence['instances']), 'instance')}")
        outcome = engine.run(args.window, region, evidence, settings, args.output, steps)
    except AuthError as exc:
        if steps.done:
            steps.failed(str(exc), exc.hints)
        else:
            status(False, str(exc))
            hint(exc.hints)
        return 1

    console.print()
    render.print_report(outcome.report)
    console.print()
    status(True, f"Recommendations written to {display_path(outcome.path)}")
    if args.print_json:
        console.print_json(json.dumps(outcome.report))
    return 0


if __name__ == "__main__":
    sys.exit(main())