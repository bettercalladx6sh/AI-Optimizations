"""Hourly metric series shared by every provider: definitions, hours, coverage and fetch planning.

One datapoint per hour: the average over that hour. Only settled hours are used, i.e. hours
that ended at least SETTLE_DELAY ago, so an hourly average is final. Each hour is fetched at
most once: hours already stored, or already checked at the provider (even when it had no
data), are never requested again.

Provider modules (app/cloud/aws/ec2/metrics.py, app/cloud/azure/vm/metrics.py) define their
metrics and how to fetch them; everything here is provider-neutral.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app import config

PERIOD = timedelta(hours=1)
STATISTIC = "Average"
SETTLE_DELAY = timedelta(minutes=config.SETTLE_DELAY_MINUTES)


@dataclass(frozen=True)
class MetricDefinition:
    """Where one utilization metric comes from at the provider."""

    key: str
    label: str
    namespace: str
    metric_name: str
    unit: str
    missing_hint: str


SeriesKey = tuple[str, str]
"""(instance ID, metric key)"""


@dataclass(frozen=True)
class Datapoint:
    """The average of one metric over one hour."""

    period_start: datetime
    average: float


@dataclass(frozen=True)
class MetricSeries:
    """Hourly averages of one metric for one instance over a window."""

    instance_id: str
    metric: MetricDefinition
    window_start: datetime
    window_end: datetime
    datapoints: tuple[Datapoint, ...]

    @property
    def expected_points(self) -> int:
        return len(hours(self.window_start, self.window_end))

    @property
    def collected_points(self) -> int:
        return len(self.datapoints)

    @property
    def average(self) -> float | None:
        """Mean of the hourly averages."""
        if not self.datapoints:
            return None
        return sum(point.average for point in self.datapoints) / len(self.datapoints)

    @property
    def peak(self) -> float | None:
        """Highest hourly average."""
        return max((point.average for point in self.datapoints), default=None)


@dataclass(frozen=True)
class Coverage:
    """What is already known about one series: stored hours and hour ranges already checked."""

    stored: frozenset[datetime] = frozenset()
    checked: tuple[tuple[datetime, datetime], ...] = ()

    def known(self, hour: datetime) -> bool:
        return hour in self.stored or any(start <= hour < end for start, end in self.checked)


@dataclass(frozen=True)
class FetchRequest:
    """One span of hours to fetch for one instance and metric."""

    instance_id: str
    metric: MetricDefinition
    start: datetime
    end: datetime

    @property
    def key(self) -> SeriesKey:
        return (self.instance_id, self.metric.key)

    @property
    def hours(self) -> int:
        return len(hours(self.start, self.end))


@dataclass
class FetchResult:
    """Datapoints returned by the provider, and what it cost."""

    points: dict[SeriesKey, tuple[Datapoint, ...]] = field(default_factory=dict)
    api_calls: int = 0
    queries: int = 0

    @property
    def datapoints(self) -> int:
        return sum(len(points) for points in self.points.values())


def floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def settled_end(now: datetime | None = None) -> datetime:
    """End of the last hour whose average is final (ended at least SETTLE_DELAY ago)."""
    return floor_hour((now or datetime.now(timezone.utc)) - SETTLE_DELAY)


def hours(start: datetime, end: datetime) -> list[datetime]:
    """Start of every whole hour in [start, end)."""
    result, moment = [], floor_hour(start)
    if moment < start:
        moment += PERIOD
    while moment < end:
        result.append(moment)
        moment += PERIOD
    return result


def missing_hours(start: datetime, end: datetime, coverage: Coverage) -> list[datetime]:
    """Hours in the window that are neither stored nor already checked."""
    return [hour for hour in hours(start, end) if not coverage.known(hour)]


def plan_requests(
    instance_ids: Iterable[str],
    start: datetime,
    end: datetime,
    coverage: Mapping[SeriesKey, Coverage],
    metrics: Sequence[MetricDefinition],
) -> list[FetchRequest]:
    """One request per instance and metric that has missing hours, spanning those hours.

    Series with nothing missing are not requested at all.
    """
    requests = []
    for instance_id in sorted(set(instance_ids)):
        for metric in metrics:
            missing = missing_hours(start, end, coverage.get((instance_id, metric.key), Coverage()))
            if missing:
                requests.append(FetchRequest(instance_id, metric, missing[0], missing[-1] + PERIOD))
    return requests


def by_instance(series: Iterable[MetricSeries], metrics: Sequence[MetricDefinition]) -> dict[str, list[MetricSeries]]:
    """Group series by instance ID, in the order of `metrics` (CPU before memory)."""
    grouped: dict[str, list[MetricSeries]] = defaultdict(list)
    for item in series:
        grouped[item.instance_id].append(item)
    order = {metric.key: index for index, metric in enumerate(metrics)}
    return {iid: sorted(items, key=lambda s: order[s.metric.key]) for iid, items in grouped.items()}
