"""EC2 resource discovery and analysis."""

from app.cloud.aws.ec2.analysis import AnalysisResult, run_analysis
from app.cloud.aws.ec2.changes import InstanceChange, InstanceSnapshot, compare
from app.cloud.aws.ec2.configuration import (
    InstanceConfiguration,
    get_configurations,
    print_configurations,
)
from app.cloud.aws.ec2.cumulative import CumulativeSummary, summarize
from app.cloud.aws.ec2.instances import Ec2Instance, list_instances, print_instances
from app.cloud.aws.ec2.metrics import METRICS, MetricSeries, fetch, plan_requests
from app.cloud.aws.ec2.utilization import UtilizationReport, analyze
from app.cloud.aws.ec2.windows import ANALYSIS_WINDOWS, AnalysisWindow

__all__ = [
    "ANALYSIS_WINDOWS",
    "AnalysisResult",
    "AnalysisWindow",
    "CumulativeSummary",
    "Ec2Instance",
    "InstanceChange",
    "InstanceConfiguration",
    "InstanceSnapshot",
    "METRICS",
    "MetricSeries",
    "UtilizationReport",
    "analyze",
    "compare",
    "fetch",
    "get_configurations",
    "list_instances",
    "plan_requests",
    "print_configurations",
    "print_instances",
    "run_analysis",
    "summarize",
]