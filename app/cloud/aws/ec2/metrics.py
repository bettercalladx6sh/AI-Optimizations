"""EC2 utilization metrics from CloudWatch: hourly averages of CPU and memory.

For every instance and metric, one datapoint per hour is collected: the average over that
hour (24 datapoints per day, 720 for a 30d window). The window ends at the last complete
hour, so a partial hour is never stored.

    CPU     AWS/EC2     CPUUtilization     dimension InstanceId   (published by AWS)
    Memory  EC2/Custom  mem_used_percent   dimension InstanceId   (published by the CloudWatch agent)
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.auth import aws_auth
from app.cloud.aws.errors import aws_errors
from app.cloud.display import field

PERIOD = timedelta(hours=1)
STATISTIC = "Average"

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
        return int((self.window_end - self.window_start) / PERIOD)

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


def collection_window(duration: timedelta, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return (start, end) ending at the last complete hour, e.g. 30d → 720 whole hours."""
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return end - duration, end


def _query(query_id: str, instance_id: str, metric: MetricDefinition) -> dict[str, Any]:
    return {
        "Id": query_id,
        "MetricStat": {
            "Metric": {
                "Namespace": metric.namespace,
                "MetricName": metric.metric_name,
                "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
            },
            "Period": int(PERIOD.total_seconds()),
            "Stat": STATISTIC,
        },
        "ReturnData": True,
    }


def _batches(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def collect_metrics(
    instance_ids: Iterable[str],
    duration: timedelta,
    region: str | None = None,
    now: datetime | None = None,
) -> list[MetricSeries]:
    """Collect hourly CPU and memory averages for each instance over the window.

    Queries are batched to stay within GetMetricData limits; every page is read.
    """
    start, end = collection_window(duration, now)
    ids = sorted(set(instance_ids))
    targets = {f"q{index}": (instance_id, metric) for index, (instance_id, metric) in
               enumerate((iid, metric) for iid in ids for metric in METRICS)}
    queries = [_query(query_id, iid, metric) for query_id, (iid, metric) in targets.items()]

    expected = max(int(duration / PERIOD), 1)
    batch_size = max(1, min(MAX_QUERIES_PER_CALL, MAX_DATAPOINTS_PER_CALL // expected))
    values: dict[str, dict[datetime, float]] = defaultdict(dict)

    client = aws_auth.get_client("cloudwatch", region=region)
    paginator = client.get_paginator("get_metric_data")
    for batch in _batches(queries, batch_size):
        with aws_errors("cloudwatch:GetMetricData"):
            for page in paginator.paginate(
                MetricDataQueries=batch, StartTime=start, EndTime=end, ScanBy="TimestampAscending"
            ):
                for result in page["MetricDataResults"]:
                    for timestamp, value in zip(result["Timestamps"], result["Values"]):
                        values[result["Id"]][timestamp.astimezone(timezone.utc)] = value

    return [
        MetricSeries(
            instance_id=instance_id,
            metric=metric,
            window_start=start,
            window_end=end,
            datapoints=tuple(
                Datapoint(moment, average) for moment, average in sorted(values[query_id].items())
                if start <= moment < end
            ),
        )
        for query_id, (instance_id, metric) in targets.items()
    ]


def by_instance(series: Iterable[MetricSeries]) -> dict[str, list[MetricSeries]]:
    """Group series by instance ID, CPU before memory."""
    grouped: dict[str, list[MetricSeries]] = defaultdict(list)
    for item in series:
        grouped[item.instance_id].append(item)
    order = {metric.key: index for index, metric in enumerate(METRICS)}
    return {iid: sorted(items, key=lambda s: order[s.metric.key]) for iid, items in grouped.items()}


def summary_line(series: MetricSeries) -> str:
    """Return e.g. 'avg 12.4% · peak 45.1% · 720/720 hours'."""
    hours = f"{series.collected_points}/{series.expected_points} hours"
    if series.average is None:
        return f"{series.metric.missing_hint} · {hours}"
    coverage = series.collected_points / series.expected_points if series.expected_points else 1
    note = f" ({coverage:.1%})" if coverage < 1 else ""
    return f"avg {series.average:.1f}% · peak {series.peak:.1f}% · {hours}{note}"


def print_series_summary(series: Iterable[MetricSeries], indent: int = 4) -> None:
    """Print one line per metric: average, peak hourly average and hours collected."""
    for item in series:
        style = "" if item.average is not None else "yellow"
        field(item.metric.label, summary_line(item), style, indent=indent)