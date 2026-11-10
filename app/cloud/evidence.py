"""Rightsizing evidence shared by every provider: the parts of the evidence document that do
not depend on the cloud, and the cross-field checks every evidence document must pass.

Each provider's evidence module (app/cloud/aws/ec2/evidence.py, app/cloud/azure/vm/evidence.py)
adds its own source, configuration and metric definitions, and validates against its own JSON
Schema. The per-instance blocks built here are identical for every provider, so the reasoning
engine reads EC2 and Azure evidence the same way:

    evaluation      whether the current configuration covers the whole window, and when it will
    metrics         per metric: hours available in the window and cumulative statistics since the
                    current configuration took effect
    data_quality    issues the evaluation must take into account
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from app import config
from app.cloud.cumulative import CumulativeSummary
from app.cloud.schema import ValidationIssue
from app.cloud.timeseries import PERIOD, STATISTIC, MetricDefinition, hours
from app.cloud.utilization import CumulativeResult, SeriesReport, UtilizationReport
from app.cloud.windows import AnalysisWindow, Readiness

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


def number(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def changes_json(changes: Sequence[Any]) -> list[dict[str, Any]]:
    """Configuration changes (each with kind, significant and fields) as JSON."""
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


def evaluation_json(readiness: Readiness | None) -> dict[str, Any]:
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


def cumulative_json(summary: CumulativeSummary) -> dict[str, Any]:
    return {
        "datapoint_count": summary.datapoint_count,
        "evidence_start": timestamp(summary.evidence_start),
        "evidence_end": timestamp(summary.evidence_end),
        "value_sum": number(summary.value_sum),
        "average": number(summary.average),
        "minimum": number(summary.minimum),
        "maximum": number(summary.maximum),
        "p50": number(summary.p50),
        "p95": number(summary.p95),
        "p99": number(summary.p99),
        "standard_deviation": number(summary.standard_deviation),
        "trend": summary.trend,
        "volatility": summary.volatility,
        "coverage_percent": number(summary.coverage_percent, 2),
        "gap_count": summary.gap_count,
    }


def metrics_json(
    window_series: Mapping[str, SeriesReport],
    cumulative: Mapping[str, CumulativeResult],
    metrics: Sequence[MetricDefinition],
) -> dict[str, Any]:
    result = {}
    for metric in metrics:
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
            "cumulative": cumulative_json(summary) if summary else None,
        }
    return result


def quality_issues(
    state: str,
    evaluation: dict[str, Any],
    metrics: dict[str, Any],
    definitions: Sequence[MetricDefinition],
    window_description: str,
    window_hours: int,
) -> list[dict[str, str]]:
    issues = []
    limited_history = min(config.LIMITED_HISTORY_HOURS, window_hours)
    for metric in definitions:
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
    if state != "running":
        issues.append(("INSTANCE_STOPPED", None, ISSUE_MESSAGES["INSTANCE_STOPPED"].format(state=state)))
    return [{"code": code, "metric": metric_key, "message": message} for code, metric_key, message in issues]


def instance_blocks(
    instance_ids: Sequence[str],
    snapshots: Mapping[str, Any],
    report: UtilizationReport,
    cumulative: Sequence[CumulativeResult],
    readiness: Sequence[Readiness],
    changes: Sequence[Any],
    definitions: Sequence[MetricDefinition],
    window: AnalysisWindow,
    configuration: Callable[[str, datetime | None], dict[str, Any]],
) -> list[dict[str, Any]]:
    """One evidence block per instance; `configuration(instance_id, effective_since)` gives its
    provider-specific configuration. Snapshots need instance_id, name and state."""
    by_readiness = {item.snapshot.instance_id: item for item in readiness}
    window_series: dict[str, dict[str, SeriesReport]] = {}
    for item in report.series:
        window_series.setdefault(item.series.instance_id, {})[item.series.metric.key] = item
    by_cumulative: dict[str, dict[str, CumulativeResult]] = {}
    for item in cumulative:
        by_cumulative.setdefault(item.instance_id, {})[item.metric.key] = item
    changes_by_id: dict[str, list[Any]] = {}
    for change in changes:
        changes_by_id.setdefault(change.instance_id, []).append(change)

    span = len(hours(report.start, report.end))
    blocks = []
    for instance_id in sorted(instance_ids, key=lambda iid: (snapshots[iid].name.lower(), iid)):
        snapshot = snapshots[instance_id]
        series = by_cumulative.get(instance_id, {})
        effective_since = next(iter(series.values())).effective_since if series else None
        evaluation = evaluation_json(by_readiness.get(instance_id))
        metrics = metrics_json(window_series.get(instance_id, {}), series, definitions)
        blocks.append({
            "instance_id": instance_id,
            "name": snapshot.name or None,
            "state": snapshot.state,
            "configuration": configuration(instance_id, effective_since),
            "changes": changes_json(changes_by_id.get(instance_id, [])),
            "evaluation": evaluation,
            "metrics": metrics,
            "data_quality": {"issues": quality_issues(
                snapshot.state, evaluation, metrics, definitions, window.description, span)},
        })
    return blocks


def window_json(window: AnalysisWindow, report: UtilizationReport) -> dict[str, Any]:
    return {
        "key": window.key,
        "purpose": window.purpose,
        "description": window.description,
        "start": timestamp(report.start),
        "end": timestamp(report.end),
        "hours": len(hours(report.start, report.end)),
        "required_stable_days": round((report.end - report.start).total_seconds() / 86400, 4),
    }


def metric_definitions_json(definitions: Sequence[MetricDefinition]) -> list[dict[str, Any]]:
    return [
        {
            "key": metric.key,
            "label": metric.label,
            "namespace": metric.namespace,
            "metric_name": metric.metric_name,
            "statistic": STATISTIC,
            "period_seconds": int(PERIOD.total_seconds()),
            "unit": metric.unit,
        }
        for metric in definitions
    ]


def summary_json(instances: list[dict[str, Any]]) -> dict[str, Any]:
    states = Counter(instance["state"] for instance in instances)
    return {
        "instance_count": len(instances),
        "instances_by_state": dict(sorted(states.items())),
        "ready_for_evaluation": sum(1 for instance in instances if instance["evaluation"]["ready"]),
        "waiting_for_evaluation": sum(1 for instance in instances if not instance["evaluation"]["ready"]),
        "configuration_changes": sum(len(instance["changes"]) for instance in instances),
        "instances_with_issues": sum(1 for instance in instances if instance["data_quality"]["issues"]),
    }


# ---------------------------------------------------------------- validation


def consistency_issues(document: dict[str, Any]) -> list[ValidationIssue]:
    """Cross-field checks the JSON Schema cannot express (same rules for every provider)."""
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
