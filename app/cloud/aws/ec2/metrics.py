"""EC2 utilization metrics from CloudWatch: definitions and fetching of hourly averages.

    CPU     AWS/EC2     CPUUtilization     dimension InstanceId   (published by AWS)
    Memory  EC2/Custom  mem_used_percent   dimension InstanceId   (published by the CloudWatch agent)

One datapoint per hour: the Average over that hour. Only settled hours are fetched, i.e. hours
that ended at least SETTLE_DELAY ago, so a fetched average is final. Each hour is fetched at
most once: hours already stored, or already checked in CloudWatch (even when it had no data),
are never requested again. The hour and series helpers are shared with every provider
(app/cloud/timeseries.py) and re-exported here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timezone
from typing import Any

from app.auth import aws_auth
from app.cloud import timeseries
from app.cloud.aws.errors import aws_errors
from app.cloud.timeseries import (
    PERIOD,
    SETTLE_DELAY,
    STATISTIC,
    Coverage,
    Datapoint,
    FetchRequest,
    FetchResult,
    MetricDefinition,
    MetricSeries,
    SeriesKey,
    floor_hour,
    hours,
    missing_hours,
    settled_end,
)

__all__ = [  # shared names re-exported for the EC2 modules, and this module's own
    "CPU", "MEMORY", "METRICS", "METRICS_BY_KEY", "PERIOD", "SETTLE_DELAY", "STATISTIC", "Coverage",
    "Datapoint", "FetchRequest", "FetchResult", "MetricDefinition", "MetricSeries", "SeriesKey",
    "by_instance", "fetch", "floor_hour", "hours", "missing_hours", "plan_requests", "settled_end",
]

# CloudWatch GetMetricData limits per request.
MAX_QUERIES_PER_CALL = 500
MAX_DATAPOINTS_PER_CALL = 100_800

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


def plan_requests(
    instance_ids: Iterable[str],
    start: datetime,
    end: datetime,
    coverage: Mapping[SeriesKey, Coverage],
) -> list[FetchRequest]:
    """One request per instance and EC2 metric that has missing hours (see timeseries.plan_requests)."""
    return timeseries.plan_requests(instance_ids, start, end, coverage, METRICS)


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
    return timeseries.by_instance(series, METRICS)
