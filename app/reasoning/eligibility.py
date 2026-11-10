"""Evidence gate: decide, without AI, whether an instance has enough evidence to be evaluated.

An instance is evaluated only when every rule passes:
    1. it is running (a stopped instance produces no utilization evidence)
    2. its current configuration covers the whole analysis window (the window holds no hour
       from before its launch or last significant configuration change)
    3. CPU and memory each have datapoints covering at least MIN_COVERAGE_PERCENT of the
       window and at least MIN_EVIDENCE_HOURS hours (both in app/config.py)

Otherwise the decision is INSUFFICIENT_DATA: no size is recommended and the user is told
exactly what is missing and when to run the analysis again. Missing data is never treated
as zero utilization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import MIN_COVERAGE_PERCENT, MIN_EVIDENCE_HOURS

METRIC_LABELS = {"cpu_utilization": "CPU", "memory_utilization": "Memory"}
EC2_MEMORY_EVIDENCE = "Memory utilization (CloudWatch agent publishing EC2/Custom mem_used_percent)"


@dataclass(frozen=True)
class MetricEvidence:
    """One metric of one instance, as the evidence reports it."""

    key: str
    label: str
    expected_hours: float
    available_hours: float
    coverage_percent: float
    datapoints: int
    average: float | None
    p95: float | None
    maximum: float | None
    trend: str | None
    volatility: str | None

    @classmethod
    def from_evidence(cls, key: str, data: dict[str, Any]) -> MetricEvidence:
        window, stats = data["window"], data["cumulative"] or {}
        return cls(
            key=key,
            label=METRIC_LABELS.get(key, key),
            expected_hours=float(window["expected_hours"]),
            available_hours=float(window["available_hours"]),
            coverage_percent=float(window["coverage_percent"]),
            datapoints=int(stats.get("datapoint_count") or 0),
            average=stats.get("average"),
            p95=stats.get("p95"),
            maximum=stats.get("maximum"),
            trend=stats.get("trend"),
            volatility=stats.get("volatility"),
        )

    @property
    def sufficient(self) -> bool:
        return (
            self.datapoints > 0
            and self.p95 is not None
            and self.available_hours >= MIN_EVIDENCE_HOURS
            and self.coverage_percent >= MIN_COVERAGE_PERCENT
        )


@dataclass(frozen=True)
class Eligibility:
    """The gate result for one instance."""

    eligible: bool
    reasons: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    action: str
    eligible_after: str | None


def metrics_of(instance: dict[str, Any]) -> dict[str, MetricEvidence]:
    return {key: MetricEvidence.from_evidence(key, data) for key, data in instance["metrics"].items()}


def _metric_reason(metric: MetricEvidence, monitor: str) -> str | None:
    if metric.datapoints == 0 or metric.p95 is None:
        return f"{metric.label} metric has no datapoints in {monitor} for this configuration."
    if metric.available_hours < MIN_EVIDENCE_HOURS:
        return (
            f"{metric.label} evidence covers {metric.available_hours:.1f} / {metric.expected_hours:.1f} hours, "
            f"below the minimum of {MIN_EVIDENCE_HOURS:g} hour(s)."
        )
    if metric.coverage_percent < MIN_COVERAGE_PERCENT:
        return (
            f"{metric.label} evidence covers {metric.available_hours:.1f} / {metric.expected_hours:.1f} hours "
            f"({metric.coverage_percent:.1f}%), below the required {MIN_COVERAGE_PERCENT:g}% coverage."
        )
    return None


def check(
    instance: dict[str, Any],
    window: dict[str, Any],
    monitor: str = "CloudWatch",
    memory_evidence: str = EC2_MEMORY_EVIDENCE,
) -> Eligibility:
    """Apply the evidence rules to one evidence instance; `window` is the evidence analysis_window.

    `monitor` names where metrics come from, `memory_evidence` what collects memory (per provider).
    """
    state = instance["state"]
    evaluation = instance["evaluation"]
    span = f"{window['key']} window ({window['description']}, {window['hours']} hours)"

    if state != "running":
        return Eligibility(
            eligible=False,
            reasons=(f"Instance is {state}; utilization cannot be measured while it is not running.",),
            missing_evidence=("CPU and memory utilization while the instance is running",),
            action="Start the instance, or exclude it, then run the analysis again.",
            eligible_after=None,
        )

    if not evaluation["ready"]:
        what = {"changed": "Configuration changed", "added": "Instance was added", "launched": "Instance was launched"}
        since = what.get(evaluation["reason"], "Evidence restarted")
        return Eligibility(
            eligible=False,
            reasons=(
                f"{since} at {evaluation['since']}; the {span} still contains hours from before it.",
            ),
            missing_evidence=(f"Evidence for the current configuration covering the whole {span}",),
            action=f"Wait until {evaluation['ready_at']}, then run the analysis again.",
            eligible_after=evaluation["ready_at"],
        )

    metrics = metrics_of(instance)
    reasons = [reason for metric in metrics.values() if (reason := _metric_reason(metric, monitor))]
    if reasons:
        missing = []
        for metric in metrics.values():
            if metric.datapoints == 0 and metric.key == "memory_utilization":
                missing.append(memory_evidence)
            elif not metric.sufficient:
                missing.append(f"{metric.label} utilization covering at least {MIN_COVERAGE_PERCENT:g}% of the window")
        return Eligibility(
            eligible=False,
            reasons=tuple(reasons),
            missing_evidence=tuple(missing),
            action=f"Collect verified {monitor} metrics for the full window before evaluating capacity.",
            eligible_after=None,
        )

    return Eligibility(
        eligible=True,
        reasons=("CPU and memory evidence meet the coverage requirements for this window.",),
        missing_evidence=(),
        action="",
        eligible_after=None,
    )