"""EC2 rightsizing evidence: a structured JSON document built from one analysis run.

The document is the input for AI evaluation and rightsizing decisions. It contains, per
instance (terminated ones excluded):
    configuration   specs of the current configuration version (type, vCPU, memory, storage, ...)
    changes         configuration changes detected in this run
    evaluation      whether the current configuration covers the whole window (no hour from
                    before the last launch or change), and when it will if not
    metrics         per metric: hours available in the window, cumulative statistics since the
                    current configuration took effect, and the data status
    data_quality    issues the evaluation must take into account (missing memory metric, ...)

Network identifiers, key pairs and IAM profiles are not included: they are not needed for
rightsizing. The format is defined by schemas/ec2_evidence.schema.json (SCHEMA_VERSION) and
checked by validate_evidence() before evidence is written or used. Evidence is built in memory
for each run and is not stored in the database. The provider-neutral parts (evaluation,
metrics, data quality, consistency checks) are shared with Azure (app/cloud/evidence.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.cloud.aws.ec2.analysis import AnalysisResult
from app.cloud.aws.ec2.changes import configuration_version
from app.cloud.aws.ec2.configuration import InstanceConfiguration
from app.cloud.aws.ec2.metrics import METRICS
from app.cloud.evidence import (
    consistency_issues,
    instance_blocks,
    metric_definitions_json,
    number,
    summary_json,
    timestamp,
    window_json,
)
from app.cloud.schema import ValidationIssue, validate_document

__all__ = ["SCHEMA_PATH", "SCHEMA_VERSION", "build_evidence", "timestamp", "validate_evidence"]

SCHEMA_VERSION = "1.0"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "ec2_evidence.schema.json"


def _configuration(config: InstanceConfiguration, version: str, effective_since: datetime | None) -> dict[str, Any]:
    spec = config.type_spec
    family, _, size = config.instance_type.partition(".")
    return {
        "version": version,
        "effective_since": timestamp(effective_since),
        "instance_type": config.instance_type,
        "family": family,
        "size": size,
        "vcpus": spec.vcpus if spec else None,
        "memory_gib": number(spec.memory_gib, 3) if spec else None,
        "network_performance": (spec.network or None) if spec else None,
        "burstable": spec.burstable if spec else None,
        "current_generation": spec.current_generation if spec else None,
        "architecture": config.architecture or None,
        "platform": config.platform or None,
        "lifecycle": config.lifecycle or None,
        "tenancy": config.tenancy or None,
        "availability_zone": config.availability_zone or None,
        "ebs_optimized": config.ebs_optimized,
        "detailed_monitoring": config.detailed_monitoring,
        "launch_time": timestamp(config.launch_time),
        "storage": {
            "total_gib": sum(volume.size_gib or 0 for volume in config.volumes),
            "volumes": [
                {
                    "volume_id": volume.volume_id,
                    "device": volume.device,
                    "size_gib": volume.size_gib,
                    "volume_type": volume.volume_type or None,
                    "iops": volume.iops,
                    "throughput_mibps": volume.throughput,
                    "encrypted": volume.encrypted,
                    "delete_on_termination": volume.delete_on_termination,
                }
                for volume in config.volumes
            ],
        },
        "tags": dict(config.tags),
    }


def build_evidence(result: AnalysisResult, generated_at: datetime | None = None) -> dict[str, Any]:
    """Build the evidence document for every non-terminated instance of an analysis run."""
    generated_at = generated_at or datetime.now(timezone.utc)
    report = result.utilization
    instances = instance_blocks(
        [item.snapshot.instance_id for item in result.readiness],
        result.snapshots, report, result.cumulative, result.readiness, result.changes, METRICS, result.window,
        lambda iid, since: _configuration(
            result.configurations[iid], configuration_version(result.snapshots[iid]), since),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": str(uuid.uuid4()),
        "generated_at": timestamp(generated_at),
        "source": {
            "provider": "aws",
            "service": "ec2",
            "account_id": result.account_id,
            "region": result.region,
            "analysis_run_id": result.run_id,
        },
        "analysis_window": window_json(result.window, report),
        "metric_definitions": metric_definitions_json(METRICS),
        "collection": {
            "cloudwatch_api_calls": result.fetched.api_calls,
            "cloudwatch_metric_queries": result.fetched.queries,
            "datapoints_expected": report.total("expected"),
            "datapoints_available": report.total("available"),
            "datapoints_fetched_now": report.total("fetched_now"),
            "hours_without_data": report.total("no_data"),
        },
        "summary": summary_json(instances),
        "instances": instances,
    }


def validate_evidence(document: dict[str, Any]) -> list[ValidationIssue]:
    """Check evidence against the JSON Schema, then the cross-field rules; return every issue."""
    issues = validate_document(document, SCHEMA_PATH)
    if issues:
        return issues
    return consistency_issues(document)