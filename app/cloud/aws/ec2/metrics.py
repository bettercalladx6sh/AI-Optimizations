"""EC2 utilization metrics from CloudWatch: definitions, planning and fetching of hourly averages.

    CPU     AWS/EC2     CPUUtilization     dimension InstanceId   (published by AWS)
    Memory  EC2/Custom  mem_used_percent   dimension InstanceId   (published by the CloudWatch agent)

One datapoint per hour: the Average over that hour. Only settled hours are fetched, i.e. hours
that ended at least SETTLE_DELAY ago, so a fetched average is final. Each hour is fetched at
most once: hours already stored, or already checked in CloudWatch (even when it had no data),
are never requested again.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.auth import aws_auth
from app.cloud.aws.errors import aws_errors

PERIOD = timedelta(hours=1)
STATISTIC = "Average"
SETTLE_DELAY = timedelta(minutes=15)

# CloudWatch GetMetricData limits per request.
MAX_QUERIES_PER_CALL = 500
MAX_DATAPOINTS_PER_CALL = 100_800


@dataclass(frozen=True)
class MetricDefinition:
    """Where one utilization metric lives in CloudWatch."""

    key: str
    label: str
    namespace: str
    metric_name: str
    unit: str
    missing_hint: str


CPU = MetricDefinition(
    key="cpu_utilization",
    label="CPU",
    namespace="AWS/EC2",
    metric_name="CPUUtilization",
    unit="Percent",
    missing_hint="no CPU data; the instance was not running in this window",
)
MEMORY = MetricDefinition(
    key="memory_utilization",
    label="Memory",
    namespace="EC2/Custom",
    metric_name="mem_used_percent",
    unit="Percent",
    missing_hint="no memory data; the CloudWatch agent is not publishing EC2/Custom mem_used_percent",
)
METRICS = (CPU, MEMORY)
METRICS_BY_KEY = {metric.key: metric for metric in METRICS}

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
    """What is already known about one series: stored hours and hour ranges checked in CloudWatch."""

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
    """Datapoints returned by CloudWatch, and what it cost."""

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
) -> list[FetchRequest]:
    """One request per instance and metric that has missing hours, spanning those hours.

    Series with nothing missing are not requested at all.
    """
    requests = []
    for instance_id in sorted(set(instance_ids)):
        for metric in METRICS:
            missing = missing_hours(start, end, coverage.get((instance_id, metric.key), Coverage()))
            if missing:
                requests.append(FetchRequest(instance_id, metric, missing[0], missing[-1] + PERIOD))
    return requests


def _query(query_id: str, request: FetchRequest) -> dict[str, Any]:
    return {
        "Id": query_id,
        "MetricStat": {
            "Metric": {
                "Namespace": request.metric.namespace,
                "MetricName": request.metric.metric_name,
                "Dimensions": [{"Name": "InstanceId", "Value": request.instance_id}],
            },
            "Period": int(PERIOD.total_seconds()),
            "Stat": STATISTIC,
        },
        "ReturnData": True,
    }


def _batches(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def fetch(requests: Iterable[FetchRequest], region: str | None = None) -> FetchResult:
    """Fetch the requested spans with as few GetMetricData calls as possible.

    Requests with the same span share calls; each call stays within the query and
    datapoint limits, and every page is read. No requests means no calls.
    """
    by_span: dict[tuple[datetime, datetime], list[FetchRequest]] = defaultdict(list)
    for request in requests:
        by_span[(request.start, request.end)].append(request)

    result = FetchResult()
    if not by_span:
        return result

    paginator = aws_auth.get_client("cloudwatch", region=region).get_paginator("get_metric_data")
    for (start, end), span_requests in sorted(by_span.items()):
        span_hours = max(len(hours(start, end)), 1)
        batch_size = max(1, min(MAX_QUERIES_PER_CALL, MAX_DATAPOINTS_PER_CALL // span_hours))
        for batch in _batches(span_requests, batch_size):
            targets = {f"q{index}": request for index, request in enumerate(batch)}
            values: dict[str, dict[datetime, float]] = defaultdict(dict)
            with aws_errors("cloudwatch:GetMetricData"):
                for page in paginator.paginate(
                    MetricDataQueries=[_query(query_id, request) for query_id, request in targets.items()],
                    StartTime=start,
                    EndTime=end,
                    ScanBy="TimestampAscending",
                ):
                    result.api_calls += 1
                    for item in page["MetricDataResults"]:
                        for timestamp, value in zip(item["Timestamps"], item["Values"]):
                            values[item["Id"]][timestamp.astimezone(timezone.utc)] = value
            result.queries += len(batch)
            for query_id, request in targets.items():
                result.points[request.key] = tuple(
                    Datapoint(moment, average)
                    for moment, average in sorted(values[query_id].items())
                    if start <= moment < end
                )
    return result


def by_instance(series: Iterable[MetricSeries]) -> dict[str, list[MetricSeries]]:
    """Group series by instance ID, CPU before memory."""
    grouped: dict[str, list[MetricSeries]] = defaultdict(list)
    for item in series:
        grouped[item.instance_id].append(item)
    order = {metric.key: index for index, metric in enumerate(METRICS)}
    return {iid: sorted(items, key=lambda s: order[s.metric.key]) for iid, items in grouped.items()}