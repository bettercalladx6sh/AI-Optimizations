"""Cumulative metric statistics: a summary of every hourly datapoint of a series.

Pure calculation with no storage or AWS calls. The raw hourly averages are authoritative:
a summary can always be rebuilt from them.

For each series the summary gives the cumulative average (sum of hourly averages / number of
hours), minimum, maximum, p50/p95/p99, standard deviation, trend, volatility, coverage and gaps.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, pstdev

from app.cloud.aws.ec2.metrics import PERIOD, Datapoint

TREND_CHANGE_PERCENT = 10.0       # second half vs first half: >= +10% GROWING, <= -10% DECLINING
VOLATILITY_HIGH = 0.50            # coefficient of variation (std dev / average)
VOLATILITY_MODERATE = 0.20


@dataclass(frozen=True)
class CumulativeSummary:
    """Statistics over every datapoint of one series."""

    evidence_start: datetime | None
    evidence_end: datetime | None
    datapoint_count: int
    value_sum: float
    average: float | None
    minimum: float | None
    maximum: float | None
    p50: float | None
    p95: float | None
    p99: float | None
    standard_deviation: float | None
    trend: str | None
    volatility: str | None
    coverage_percent: float
    gap_count: int


EMPTY = CumulativeSummary(None, None, 0, 0.0, None, None, None, None, None, None, None, None, None, 0.0, 0)


def percentile(ordered: Sequence[float], percent: float) -> float | None:
    """Linear-interpolated percentile of already sorted values."""
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def trend(values: Sequence[float]) -> str | None:
    """Compare the average of the second half with the first half (in time order)."""
    if len(values) < 2:
        return None
    midpoint = len(values) // 2
    first, second = mean(values[:midpoint]), mean(values[midpoint:])
    if first == 0:
        return "STABLE" if second == 0 else "GROWING"
    change = (second - first) / abs(first) * 100.0
    if change >= TREND_CHANGE_PERCENT:
        return "GROWING"
    if change <= -TREND_CHANGE_PERCENT:
        return "DECLINING"
    return "STABLE"


def volatility(values: Sequence[float]) -> str | None:
    """Classify spread by the coefficient of variation (std dev / average)."""
    if len(values) < 2:
        return None
    average = mean(values)
    if average == 0:
        return "STABLE"
    coefficient = pstdev(values) / abs(average)
    if coefficient >= VOLATILITY_HIGH:
        return "HIGH"
    if coefficient >= VOLATILITY_MODERATE:
        return "MODERATE"
    return "LOW"


def gap_count(moments: Sequence[datetime], period: timedelta = PERIOD) -> int:
    """Missing periods between consecutive datapoints."""
    return sum(
        max(int((current - previous) / period) - 1, 0)
        for previous, current in zip(moments, moments[1:])
    )


def summarize(datapoints: Sequence[Datapoint], period: timedelta = PERIOD) -> CumulativeSummary:
    """Calculate the cumulative summary of a series of hourly datapoints."""
    if not datapoints:
        return EMPTY

    points = sorted(datapoints, key=lambda point: point.period_start)
    values = [point.average for point in points]
    moments = [point.period_start for point in points]
    ordered = sorted(values)
    count = len(values)
    total = float(sum(values))
    expected = int((moments[-1] - moments[0]) / period) + 1

    return CumulativeSummary(
        evidence_start=moments[0],
        evidence_end=moments[-1],
        datapoint_count=count,
        value_sum=total,
        average=total / count,
        minimum=ordered[0],
        maximum=ordered[-1],
        p50=percentile(ordered, 50.0),
        p95=percentile(ordered, 95.0),
        p99=percentile(ordered, 99.0),
        standard_deviation=pstdev(values) if count >= 2 else 0.0,
        trend=trend(values),
        volatility=volatility(values),
        coverage_percent=min(count / expected * 100.0, 100.0),
        gap_count=gap_count(moments, period),
    )


def stats_line(summary: CumulativeSummary) -> str:
    """Return e.g. 'avg 12.4% · p50 11.0% · p95 30.1% · p99 41.0% · min 2.0% · max 45.1%'."""
    return (
        f"avg {summary.average:.1f}% · p50 {summary.p50:.1f}% · p95 {summary.p95:.1f}% · "
        f"p99 {summary.p99:.1f}% · min {summary.minimum:.1f}% · max {summary.maximum:.1f}%"
    )


def shape_line(summary: CumulativeSummary) -> str:
    """Return e.g. 'σ 3.2 · trend STABLE · volatility LOW · 217 h · coverage 100.0% · 0 gaps'."""
    gaps = f"{summary.gap_count} gap{'s' if summary.gap_count != 1 else ''}"
    return (
        f"σ {summary.standard_deviation:.2f} · trend {summary.trend or '—'} · "
        f"volatility {summary.volatility or '—'} · {summary.datapoint_count} h · "
        f"coverage {summary.coverage_percent:.1f}% · {gaps}"
    )