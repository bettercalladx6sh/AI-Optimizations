"""EC2 resource discovery and analysis."""

from app.cloud.aws.ec2.analysis import ANALYSIS_WINDOWS, AnalysisResult, run_analysis
from app.cloud.aws.ec2.changes import InstanceChange, InstanceSnapshot, compare
from app.cloud.aws.ec2.configuration import (
    InstanceConfiguration,
    get_configurations,
    print_configurations,
)
from app.cloud.aws.ec2.instances import Ec2Instance, list_instances, print_instances

__all__ = [
    "ANALYSIS_WINDOWS",
    "AnalysisResult",
    "Ec2Instance",
    "InstanceChange",
    "InstanceConfiguration",
    "InstanceSnapshot",
    "compare",
    "get_configurations",
    "list_instances",
    "print_configurations",
    "print_instances",
    "run_analysis",
]