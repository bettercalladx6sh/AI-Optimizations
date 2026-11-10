"""Azure VM utilization metrics: definitions and fetching of hourly averages.

    CPU     Azure Monitor platform metric  Percentage CPU             (published by Azure)
    Memory  Log Analytics workspace        InsightsMetrics Memory/AvailableMB, converted to used %
                                           (VM insights / Azure Monitor Agent), when
                                           AZURE_LOG_ANALYTICS_WORKSPACE_ID is set
            otherwise Azure Monitor        Available Memory Bytes, converted to used % with the
                                           VM size's memory

One datapoint per hour (the average over that hour), settled hours only, and each hour is
fetched at most once (app/cloud/timeseries.py). Platform metrics are read per VM with at most
MAX_SPAN per call; Log Analytics memory is read for up to LOGS_BATCH VMs per query.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from app.auth import azure_auth
from app.cloud import timeseries
from app.cloud.azure import rest
from app.cloud.timeseries import (
    Coverage,
    Datapoint,
    FetchRequest,
    FetchResult,
    MetricDefinition,
    SeriesKey,
)

METRICS_API_VERSION = "2023-10-01"
NAMESPACE = "Microsoft.Compute/virtualMachines"
MAX_SPAN = timedelta(days=30)        # longest time range requested per platform-metric call
LOGS_BATCH = 100                     # VMs per Log Analytics query
BYTES_PER_GIB = 1024 ** 3

CPU = MetricDefinition(
    key="cpu_utilization",
    label="CPU",
    namespace=NAMESPACE,
    metric_name="Percentage CPU",
    unit="Percent",
    missing_hint="no CPU data; the VM was not running in this window",
)
MEMORY_PLATFORM = MetricDefinition(
    key="memory_utilization",
    label="Memory",
    namespace=NAMESPACE,
    metric_name="Available Memory Bytes",
    unit="Percent",
    missing_hint="no memory data; Azure Monitor has no Available Memory Bytes for this VM",
)
MEMORY_LOGS = MetricDefinition(
    key="memory_utilization",
    label="Memory",
    namespace="Log Analytics InsightsMetrics",
    metric_name="Memory/AvailableMB",
    unit="Percent",
    missing_hint="no memory data; the VM sends no VM insights memory (InsightsMetrics Memory/AvailableMB) "
                 "to the Log Analytics workspace",
)

MEMORY_QUERY = """
InsightsMetrics
| where TimeGenerated >= datetime({start}) and TimeGenerated < datetime({end})
| where Namespace == "Memory" and Name == "AvailableMB"
| where _ResourceId in~ ({resource_ids})
| extend total_mb = todouble(parse_json(Tags)["vm.azm.ms/memorySizeMB"])
| where total_mb > 0
| summarize used_percent = avg(100.0 * (1.0 - Val / total_mb))
    by resource_id = tolower(_ResourceId), hour = bin(TimeGenerated, 1h)
"""


@dataclass(frozen=True)
class MetricSource:
    """What a fetch needs to know about one VM."""

    resource_id: str
    memory_gib: float | None


def workspace_id() -> str:
    return azure_auth.load_env()["AZURE_LOG_ANALYTICS_WORKSPACE_ID"]


@lru_cache(maxsize=1)
def metrics() -> tuple[MetricDefinition, MetricDefinition]:
    """CPU and memory; memory comes from Log Analytics when a workspace is configured."""
    return (CPU, MEMORY_LOGS if workspace_id() else MEMORY_PLATFORM)


def memory_source() -> str:
    """'log_analytics' or 'platform_metric': where this run reads memory from."""
    return "log_analytics" if metrics()[1] is MEMORY_LOGS else "platform_metric"


MEMORY_SOURCE_LABELS = {"log_analytics": "Log Analytics (VM insights)", "platform_metric": "platform metric"}


def plan_requests(
    instance_ids: Iterable[str],
    start: datetime,
    end: datetime,
    coverage: Mapping[SeriesKey, Coverage],
) -> list[FetchRequest]:
    """One request per VM and metric that has missing hours (see timeseries.plan_requests)."""
    return timeseries.plan_requests(instance_ids, start, end, coverage, metrics())


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _spans(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    spans, moment = [], start
    while moment < end:
        spans.append((moment, min(moment + MAX_SPAN, end)))
        moment += MAX_SPAN
    return spans


def _platform_points(request: FetchRequest, source: MetricSource, result: FetchResult) -> tuple[Datapoint, ...]:
    """Hourly averages of one platform metric for one VM (memory converted to used %)."""
    if request.metric is MEMORY_PLATFORM and not source.memory_gib:
        return ()                                        # cannot convert bytes without the VM's memory size
    values: dict[datetime, float] = {}
    for start, end in _spans(request.start, request.end):
        response = rest.arm_get(
            f"{source.resource_id}/providers/Microsoft.Insights/metrics", METRICS_API_VERSION,
            "Microsoft.Insights/metrics/read",
            {
                "metricnames": request.metric.metric_name,
                "metricnamespace": NAMESPACE,
                "timespan": f"{_iso(start)}/{_iso(end)}",
                "interval": "PT1H",
                "aggregation": "Average",
            },
            permission="the Monitoring Reader role on the subscription",
        )
        result.api_calls += 1
        for metric in response.get("value", []):
            for series in metric.get("timeseries", []):
                for point in series.get("data", []):
                    average = point.get("average")
                    if average is None:
                        continue                         # Azure lists every hour; no average = no data
                    values[_moment(point["timeStamp"])] = float(average)
    total = (source.memory_gib or 0) * BYTES_PER_GIB
    points = []
    for moment, average in sorted(values.items()):
        if not request.start <= moment < request.end:
            continue
        if request.metric is MEMORY_PLATFORM:
            average = min(max(100.0 * (1.0 - average / total), 0.0), 100.0)
        points.append(Datapoint(moment, average))
    return tuple(points)


def _logs_points(requests: Sequence[FetchRequest], sources: Mapping[str, MetricSource],
                 result: FetchResult) -> None:
    """Memory used % from Log Analytics, one query per span and batch of VMs."""
    by_span: dict[tuple[datetime, datetime], list[FetchRequest]] = defaultdict(list)
    for request in requests:
        by_span[(request.start, request.end)].append(request)
    workspace = workspace_id()
    for (start, end), span_requests in sorted(by_span.items()):
        for index in range(0, len(span_requests), LOGS_BATCH):
            batch = span_requests[index:index + LOGS_BATCH]
            by_resource = {sources[request.instance_id].resource_id.lower(): request for request in batch}
            ids = ", ".join(f'"{resource_id}"' for resource_id in sorted(by_resource))
            rows = rest.log_analytics_query(
                workspace,
                MEMORY_QUERY.format(start=_iso(start), end=_iso(end), resource_ids=ids),
                f"{_iso(start)}/{_iso(end)}",
            )
            result.api_calls += 1
            points: dict[str, list[Datapoint]] = defaultdict(list)
            for row in rows:
                request = by_resource.get(str(row["resource_id"]).lower())
                if request is None or row.get("used_percent") is None:
                    continue
                moment = _moment(row["hour"])
                if start <= moment < end:
                    value = min(max(float(row["used_percent"]), 0.0), 100.0)
                    points[request.instance_id].append(Datapoint(moment, value))
            for request in batch:
                result.points[request.key] = tuple(sorted(points.get(request.instance_id, ()),
                                                          key=lambda point: point.period_start))
            result.queries += len(batch)


def fetch(requests: Iterable[FetchRequest], sources: Mapping[str, MetricSource]) -> FetchResult:
    """Fetch the requested spans: platform metrics per VM, Log Analytics memory in batches.

    No requests means no calls.
    """
    result = FetchResult()
    logs: list[FetchRequest] = []
    for request in requests:
        if request.metric is MEMORY_LOGS:
            logs.append(request)
            continue
        result.points[request.key] = _platform_points(request, sources[request.instance_id], result)
        result.queries += 1
    if logs:
        _logs_points(logs, sources, result)
    return result


__all__ = ["CPU", "MEMORY_LOGS", "MEMORY_PLATFORM", "MetricSource", "fetch", "memory_source", "metrics", "plan_requests", "workspace_id"]
