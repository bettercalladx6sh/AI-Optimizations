"""The last three pipeline steps shared by every provider: summarize the evidence that was
built in memory, validate it, and write it to its JSON file (replaced in place, and only after
it passed validation). Also checks an existing evidence file against its schema.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.auth.common import AuthError, console, status
from app.cloud.display import StepPrinter, plural
from app.cloud.files import PROJECT_ROOT, display_path, write_json
from app.cloud.schema import EvidenceValidationError, ValidationIssue

EVIDENCE_DIR = PROJECT_ROOT / "evidence"
MAX_ISSUES_SHOWN = 25
OUTPUT_STEPS = ("Prepare evidence", "Validate evidence schema", "Update evidence file")

Validator = Callable[[dict[str, Any]], list[ValidationIssue]]


def evidence_path(document: dict[str, Any]) -> Path:
    """evidence/<provider>/<service>/<account>/<region>/<window>.json under the project root."""
    source = document["source"]
    return (EVIDENCE_DIR / source["provider"] / source["service"] / source["account_id"] / source["region"]
            / f"{document['analysis_window']['key']}.json")


def print_issues(issues: list[ValidationIssue]) -> None:
    for issue in issues[:MAX_ISSUES_SHOWN]:
        console.print(f"    [red]•[/] {issue}", highlight=False)
    if len(issues) > MAX_ISSUES_SHOWN:
        console.print(f"    [dim]… and {len(issues) - MAX_ISSUES_SHOWN} more[/]")


def publish(
    evidence: dict[str, Any],
    validate: Validator,
    schema_version: str,
    steps: StepPrinter,
    output: Path | None = None,
    write_file: bool = True,
) -> Path | None:
    """Report the prepared evidence, validate it, then write it; return the file written (if any)."""
    titles = OUTPUT_STEPS
    size = len(json.dumps(evidence))
    issue_count = sum(len(instance["data_quality"]["issues"]) for instance in evidence["instances"])
    steps(titles[0], f"{plural(len(evidence['instances']), 'instance')} · "
                     f"{plural(issue_count, 'data-quality issue')} · {size / 1024:.1f} KB")

    issues = validate(evidence)
    if issues:
        raise EvidenceValidationError(
            f"evidence failed validation with {plural(len(issues), 'issue')}; no file was written",
            [str(issue) for issue in issues[:MAX_ISSUES_SHOWN]],
        )
    steps(titles[1], f"schema {schema_version} · valid")

    if not write_file:
        steps(titles[2], "skipped (--no-file) · evidence kept in memory for this run")
        return None
    path = output or evidence_path(evidence)
    updated = write_json(evidence, path)
    steps(titles[2], f"{'updated' if updated else 'created'} {display_path(path)}")
    return path


def validate_file(path: Path, validate: Validator, schema_version: str) -> int:
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
        issues = validate(document)
    except AuthError as exc:
        status(False, str(exc))
        return 1
    except (KeyError, TypeError, ValueError) as exc:
        status(False, f"{path}: unexpected structure ({exc})")
        return 1
    if issues:
        status(False, f"{path}: {plural(len(issues), 'issue')} against schema {schema_version}")
        print_issues(issues)
        return 1
    status(True, f"{path}: valid evidence (schema {schema_version}, {plural(len(document['instances']), 'instance')})")
    return 0
