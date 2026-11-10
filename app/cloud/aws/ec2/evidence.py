"""EC2 rightsizing evidence: a structured JSON document built from one analysis run.

The document is the input for AI evaluation and rightsizing decisions. It contains, per
instance (terminated ones excluded):
    configuration   specs of the current configuration version (type, vCPU, memory, storage, ...)
    changes         configuration changes detected in this run
    evaluation      whether the current configuration covers the whole window (no hour from
                    before the last launch or change), and when it will if not
    metrics         per metric: hours available in the window, cumulative statistics since the
                    current configuration took effect, and the data status
    data_quality    issues the evaluation must take into account (missing memory metric, ...)

Network identifiers, key pairs and IAM profiles are not included: they are not needed for
rightsizing. The format is defined by schemas/ec2_evidence.schema.json (SCHEMA_VERSION) and
checked by validate_evidence() before evidence is written or used. Evidence is built in memory
for each run and is not stored in the database.
"""

from __future__ import annotations

import math
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import config
from app.cloud.aws.ec2.analysis import AnalysisResult, Readiness
from app.cloud.aws.ec2.changes import InstanceChange, configuration_version
from app.cloud.aws.ec2.configuration import InstanceConfiguration
from app.cloud.aws.ec2.cumulative import CumulativeSummary
from app.cloud.aws.ec2.metrics import METRICS, PERIOD, STATISTIC, hours
from app.cloud.aws.ec2.utilization import CumulativeResult, SeriesReport
from app.cloud.schema import ValidationIssue, validate_document

SCHEMA_VERSION = "1.0"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "ec2_evidence.schema.json"

TOLERANCE = 1e-6

ISSUE_MESSAGES = {
    "METRIC_MISSING": "{label} metric has no datapoints ({hint})",
    "LOW_COVERAGE": "{label} has datapoints for only {coverage:.1f}% of the window hours",
    "LIMITED_HISTORY": "{label} has only {count} hours of evidence for the current configuration",
    "RECENTLY_CHANGED": "configuration changed {since}; evaluation waits until {ready_at}, when the {window} window holds no earlier hours",
    "RECENTLY_LAUNCHED": "launched {since}; evaluation waits until {ready_at}, when the {window} window holds no earlier hours",
    "INSTANCE_STOPPED": "instance is {state}; utilization reflects only the hours it was running",
}


def timestamp(moment: datetime | None) -> str | None:
    """ISO 8601 UTC with a Z suffix, e.g. '2026-11-10T07:00:00Z'."""
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _configuration(config: InstanceConfiguration, version: str, effective_since: datetime | None) -> dict[str, Any]:
    spec = config.type_spec
    family, _, size = config.instance_type.partition(".")
    return {
        "version": version,
        "effective_since": timestamp(effective_since),
        "instance_type": config.instance_type,
        "family": family,
        "size": size,
        "vcpus": spec.vcpus if spec else None,
        "memory_gib": _number(spec.memory_gib, 3) if spec else None,
        "network_performance": (spec.network or None) if spec else None,
        "burstable": spec.burstable if spec else None,
        "current_generation": spec.current_generation if spec else None,
        "architecture": config.architecture or None,
        "platform": config.platform or None,
        "lifecycle": config.lifecycle or None,
        "tenancy": config.tenancy or None,
        "availability_zone": config.availability_zone or None,
        "ebs_optimized": config.ebs_optimized,
        "detailed_monitoring": config.detailed_monitoring,
        "launch_time": timestamp(config.launch_time),
        "storage": {
            "total_gib": sum(volume.size_gib or 0 for volume in config.volumes),
            "volumes": [
                {
                    "volume_id": volume.volume_id,
                    "device": volume.device,
                    "size_gib": volume.size_gib,
                    "volume_type": volume.volume_type or None,
                    "iops": volume.iops,
                    "throughput_mibps": volume.throughput,
                    "encrypted": volume.encrypted,
                    "delete_on_termination": volume.delete_on_termination,
                }
                for volume in config.volumes
            ],
        },
        "tags": dict(config.tags),
    }


def _changes(changes: list[InstanceChange]) -> list[dict[str, Any]]:
    return [
        {
            "kind": change.kind,
            "significant": change.significant,
            "fields": [
                {
                    "field": item.field,
                    "before": list(item.before) if isinstance(item.before, tuple) else item.before,
                    "after": list(item.after) if isinstance(item.after, tuple) else item.after,
                }
                for item in change.fields
            ],
        }
        for change in changes
    ]


def _evaluation(readiness: Readiness | None) -> dict[str, Any]:
    if readiness is None:
        return {"ready": False, "reason": "not_measured", "since": None, "ready_at": None, "remaining_hours": None}
    remaining = None
    if not readiness.ready and readiness.ready_at:
        remaining = round((readiness.ready_at - readiness.checked_at).total_seconds() / 3600, 2)
    return {
        "ready": readiness.ready,
        "reason": readiness.reason,
        "since": timestamp(readiness.since),
        "ready_at": timestamp(readiness.ready_at),
        "remaining_hours": remaining,
    }


def _cumulative(summary: CumulativeSummary) -> dict[str, Any]:
    return {
        "datapoint_count": summary.datapoint_count,
        "evidence_start": timestamp(summary.evidence_start),
        "evidence_end": timestamp(summary.evidence_end),
        "value_sum": _number(summary.value_sum),
        "average": _number(summary.average),
        "minimum": _number(summary.minimum),
        "maximum": _number(summary.maximum),
        "p50": _number(summary.p50),
        "p95": _number(summary.p95),
        "p99": _number(summary.p99),
        "standard_deviation": _number(summary.standard_deviation),
        "trend": summary.trend,
        "volatility": summary.volatility,
        "coverage_percent": _number(summary.coverage_percent, 2),
        "gap_count": summary.gap_count,
    }


def _metrics(
    window_series: dict[str, SeriesReport],
    cumulative: dict[str, CumulativeResult],
) -> dict[str, Any]:
    result = {}
    for metric in METRICS:
        window = window_series.get(metric.key)
        summary = cumulative[metric.key].summary if metric.key in cumulative else None
        expected = window.expected if window else 0
        available = window.available if window else 0
        result[metric.key] = {
            "data_status": "available" if summary and summary.datapoint_count else "no_data",
            "window": {
                "expected_hours": expected,
                "available_hours": available,
                "coverage_percent": round(available / expected * 100.0, 2) if expected else 0.0,
            },
            "cumulative": _cumulative(summary) if summary else None,
        }
    return result


def _issues(
    snapshot_state: str,
    evaluation: dict[str, Any],
    metrics: dict[str, Any],
    window_description: str,
    window_hours: int,
) -> list[dict[str, str]]:
    issues = []
    limited_history = min(config.LIMITED_HISTORY_HOURS, window_hours)
    for metric in METRICS:
        data = metrics[metric.key]
        label = metric.label
        if data["data_status"] == "no_data":
            issues.append(("METRIC_MISSING", metric.key, ISSUE_MESSAGES["METRIC_MISSING"].format(
                label=label, hint=metric.missing_hint)))
            continue
        coverage = data["window"]["coverage_percent"]
        if coverage < config.MIN_COVERAGE_PERCENT:
            issues.append(("LOW_COVERAGE", metric.key, ISSUE_MESSAGES["LOW_COVERAGE"].format(
                label=label, coverage=coverage)))
        count = data["cumulative"]["datapoint_count"]
        if count < limited_history:
            issues.append(("LIMITED_HISTORY", metric.key, ISSUE_MESSAGES["LIMITED_HISTORY"].format(
                label=label, count=count)))
    if not evaluation["ready"] and evaluation["reason"] in ("changed", "added"):
        issues.append(("RECENTLY_CHANGED", None, ISSUE_MESSAGES["RECENTLY_CHANGED"].format(
            since=evaluation["since"], ready_at=evaluation["ready_at"], window=window_description)))
    if not evaluation["ready"] and evaluation["reason"] == "launched":
        issues.append(("RECENTLY_LAUNCHED", None, ISSUE_MESSAGES["RECENTLY_LAUNCHED"].format(
            since=evaluation["since"], ready_at=evaluation["ready_at"], window=window_description)))
    if snapshot_state != "running":
        issues.append(("INSTANCE_STOPPED", None, ISSUE_MESSAGES["INSTANCE_STOPPED"].format(state=snapshot_state)))
    return [{"code": code, "metric": metric_key, "message": message} for code, metric_key, message in issues]


def build_evidence(result: AnalysisResult, generated_at: datetime | None = None) -> dict[str, Any]:
    """Build the evidence document for every non-terminated instance of an analysis run."""
    generated_at = generated_at or datetime.now(timezone.utc)
    report = result.utilization
    readiness = {item.snapshot.instance_id: item for item in result.readiness}
    window_series: dict[str, dict[str, SeriesReport]] = {}
    for item in report.series:
        window_series.setdefault(item.series.instance_id, {})[item.series.metric.key] = item
    cumulative: dict[str, dict[str, CumulativeResult]] = {}
    for item in result.cumulative:
        cumulative.setdefault(item.instance_id, {})[item.metric.key] = item
    changes_by_id: dict[str, list[InstanceChange]] = {}
    for change in result.changes:
        changes_by_id.setdefault(change.instance_id, []).append(change)

    span = len(hours(report.start, report.end))
    instances = []
    for instance_id in sorted(readiness, key=lambda iid: (result.snapshots[iid].name.lower(), iid)):
        snapshot, config = result.snapshots[instance_id], result.configurations[instance_id]
        series = cumulative.get(instance_id, {})
        effective_since = next(iter(series.values())).effective_since if series else None
        evaluation = _evaluation(readiness.get(instance_id))
        metrics = _metrics(window_series.get(instance_id, {}), series)
        instances.append({
            "instance_id": instance_id,
            "name": snapshot.name or None,
            "state": snapshot.state,
            "configuration": _configuration(config, configuration_version(snapshot), effective_since),
            "changes": _changes(changes_by_id.get(instance_id, [])),
            "evaluation": evaluation,
            "metrics": metrics,
            "data_quality": {"issues": _issues(snapshot.state, evaluation, metrics, result.window.description, span)},
        })

    states = Counter(instance["state"] for instance in instances)
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": str(uuid.uuid4()),
        "generated_at": timestamp(generated_at),
        "source": {
            "provider": "aws",
            "service": "ec2",
            "account_id": result.account_id,
            "region": result.region,
            "analysis_run_id": result.run_id,
        },
        "analysis_window": {
            "key": result.window.key,
            "purpose": result.window.purpose,
            "description": result.window.description,
            "start": timestamp(report.start),
            "end": timestamp(report.end),
            "hours": span,
            "required_stable_days": round((report.end - report.start).total_seconds() / 86400, 4),
        },
        "metric_definitions": [
            {
                "key": metric.key,
                "label": metric.label,
                "namespace": metric.namespace,
                "metric_name": metric.metric_name,
                "statistic": STATISTIC,
                "period_seconds": int(PERIOD.total_seconds()),
                "unit": metric.unit,
            }
            for metric in METRICS
        ],
        "collection": {
            "cloudwatch_api_calls": result.fetched.api_calls,
            "cloudwatch_metric_queries": result.fetched.queries,
            "datapoints_expected": report.total("expected"),
            "datapoints_available": report.total("available"),
            "datapoints_fetched_now": report.total("fetched_now"),
            "hours_without_data": report.total("no_data"),
        },
        "summary": {
            "instance_count": len(instances),
            "instances_by_state": dict(sorted(states.items())),
            "ready_for_evaluation": sum(1 for instance in instances if instance["evaluation"]["ready"]),
            "waiting_for_evaluation": sum(1 for instance in instances if not instance["evaluation"]["ready"]),
            "configuration_changes": sum(len(instance["changes"]) for instance in instances),
            "instances_with_issues": sum(1 for instance in instances if instance["data_quality"]["issues"]),
        },
        "instances": instances,
    }


# ---------------------------------------------------------------- validation


def _consistency_issues(document: dict[str, Any]) -> list[ValidationIssue]:
    """Cross-field checks the JSON Schema cannot express."""
    issues: list[ValidationIssue] = []
    instances = document["instances"]
    summary = document["summary"]
    window = document["analysis_window"]

    ids = [instance["instance_id"] for instance in instances]
    for duplicate in sorted({iid for iid in ids if ids.count(iid) > 1}):
        issues.append(ValidationIssue("$.instances", f"instance_id {duplicate} appears more than once"))

    if summary["instance_count"] != len(instances):
        issues.append(ValidationIssue("$.summary.instance_count", f"is {summary['instance_count']} but there are {len(instances)} instances"))
    if sum(summary["instances_by_state"].values()) != len(instances):
        issues.append(ValidationIssue("$.summary.instances_by_state", "counts do not add up to instance_count"))
    if summary["ready_for_evaluation"] + summary["waiting_for_evaluation"] != len(instances):
        issues.append(ValidationIssue("$.summary", "ready_for_evaluation + waiting_for_evaluation must equal instance_count"))

    start = datetime.fromisoformat(window["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(window["end"].replace("Z", "+00:00"))
    if end < start:
        issues.append(ValidationIssue("$.analysis_window", "end is before start"))
    elif window["hours"] != round((end - start).total_seconds() / 3600):
        issues.append(ValidationIssue("$.analysis_window.hours", "does not match start and end"))
    elif not math.isclose(window["required_stable_days"], window["hours"] / 24, abs_tol=1e-3):
        issues.append(ValidationIssue("$.analysis_window.required_stable_days", "must equal the window span in days"))

    defined = {definition["key"] for definition in document["metric_definitions"]}
    for index, instance in enumerate(instances):
        base = f"$.instances[{index}]"
        if set(instance["metrics"]) != defined:
            issues.append(ValidationIssue(f"{base}.metrics", f"keys must be exactly {sorted(defined)}"))
        for key, data in instance["metrics"].items():
            path = f"{base}.metrics.{key}"
            window_data = data["window"]
            if window_data["available_hours"] > window_data["expected_hours"]:
                issues.append(ValidationIssue(f"{path}.window", "available_hours exceeds expected_hours"))
            stats = data["cumulative"]
            if stats is None or stats["datapoint_count"] == 0:
                if data["data_status"] != "no_data":
                    issues.append(ValidationIssue(f"{path}.data_status", "must be no_data when there are no datapoints"))
                continue
            if data["data_status"] != "available":
                issues.append(ValidationIssue(f"{path}.data_status", "must be available when there are datapoints"))
            ordered = [stats[name] for name in ("minimum", "p50", "p95", "p99", "maximum")]
            if any(later < earlier - TOLERANCE for earlier, later in zip(ordered, ordered[1:])):
                issues.append(ValidationIssue(f"{path}.cumulative", "must satisfy minimum <= p50 <= p95 <= p99 <= maximum"))
            if not stats["minimum"] - TOLERANCE <= stats["average"] <= stats["maximum"] + TOLERANCE:
                issues.append(ValidationIssue(f"{path}.cumulative.average", "must lie between minimum and maximum"))
            expected_average = stats["value_sum"] / stats["datapoint_count"]
            if not math.isclose(stats["average"], expected_average, rel_tol=1e-3, abs_tol=1e-3):
                issues.append(ValidationIssue(f"{path}.cumulative.average", "must equal value_sum / datapoint_count"))
        evaluation = instance["evaluation"]
        if not evaluation["ready"] and evaluation["reason"] != "not_measured" and evaluation["ready_at"] is None:
            issues.append(ValidationIssue(f"{base}.evaluation.ready_at", "is required when the instance is not ready"))
    return issues


def validate_evidence(document: dict[str, Any]) -> list[ValidationIssue]:
    """Check evidence against the JSON Schema, then the cross-field rules; return every issue."""
    issues = validate_document(document, SCHEMA_PATH)
    if issues:
        return issues
    return _consistency_issues(document)