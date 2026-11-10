"""Azure VM rightsizing evidence: a structured JSON document built from one analysis run.

The same document shape as the EC2 evidence (app/cloud/evidence.py builds the per-VM
evaluation, metrics and data-quality blocks), with Azure source, configuration and collection
details:
    source          subscription, tenant, location (or "all") and analysis run
    configuration   VM size specs (vCPU, memory, family), OS, image, licence, priority,
                    placement and managed disks of the current configuration version
    collection      Azure Monitor / Log Analytics calls and where memory came from

instance_id is the VM's immutable vmId and instance_type its VM size (e.g. Standard_D2s_v3).
Network details are not included: they are not needed for rightsizing. The format is defined by
schemas/azure_vm_evidence.schema.json (SCHEMA_VERSION) and checked by validate_evidence()
before evidence is written or used. Evidence is built in memory for each run and is not stored
in the database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.cloud.azure.vm import metrics
from app.cloud.azure.vm.analysis import AnalysisResult
from app.cloud.azure.vm.changes import configuration_version
from app.cloud.azure.vm.inventory import VmConfiguration
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

__all__ = ["SCHEMA_PATH", "SCHEMA_VERSION", "build_evidence", "validate_evidence"]

SCHEMA_VERSION = "1.0"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "azure_vm_evidence.schema.json"


def _configuration(config: VmConfiguration, version: str, effective_since: datetime | None) -> dict[str, Any]:
    spec = config.size_spec
    return {
        "version": version,
        "effective_since": timestamp(effective_since),
        "instance_type": config.vm_size,
        "family": spec.family if spec else None,
        "vcpus": spec.vcpus if spec else None,
        "memory_gib": number(spec.memory_gib, 3) if spec else None,
        "burstable": spec.burstable if spec else None,
        "architecture": (spec.architecture or None) if spec else None,
        "os_type": config.os_type or None,
        "image": config.image or None,
        "marketplace_plan": config.marketplace_plan or None,
        "license_type": config.license_type or None,
        "priority": config.priority or None,
        "eviction_policy": config.eviction_policy or None,
        "location": config.vm.location,
        "zone": config.zone or None,
        "resource_group": config.vm.resource_group,
        "availability_set": config.availability_set or None,
        "scale_set": config.scale_set or None,
        "dedicated_host": config.dedicated_host,
        "time_created": timestamp(config.time_created),
        "storage": {
            "total_gib": sum(disk.size_gib or 0 for disk in config.disks),
            "disks": [
                {
                    "name": disk.name,
                    "role": disk.role,
                    "lun": disk.lun,
                    "size_gib": disk.size_gib,
                    "sku": disk.sku or None,
                    "iops": disk.iops,
                    "throughput_mbps": disk.throughput_mbps,
                    "encryption": disk.encryption or None,
                    "delete_option": disk.delete_option or None,
                }
                for disk in config.disks
            ],
        },
        "tags": dict(config.tags),
    }


def build_evidence(result: AnalysisResult, generated_at: datetime | None = None) -> dict[str, Any]:
    """Build the evidence document for every VM of an analysis run."""
    generated_at = generated_at or datetime.now(timezone.utc)
    report = result.utilization
    definitions = metrics.metrics()
    instances = instance_blocks(
        [item.snapshot.instance_id for item in result.readiness],
        result.snapshots, report, result.cumulative, result.readiness, result.changes, definitions, result.window,
        lambda iid, since: _configuration(
            result.configurations[iid], configuration_version(result.snapshots[iid]), since),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": str(uuid.uuid4()),
        "generated_at": timestamp(generated_at),
        "source": {
            "provider": "azure",
            "service": "vm",
            "account_id": result.subscription_id.lower(),
            "subscription_name": result.subscription_name,
            "tenant_id": (result.tenant_id or "").lower(),
            "region": result.location.lower(),
            "analysis_run_id": result.run_id,
        },
        "analysis_window": window_json(result.window, report),
        "metric_definitions": metric_definitions_json(definitions),
        "collection": {
            "api_calls": result.fetched.api_calls,
            "metric_queries": result.fetched.queries,
            "memory_source": metrics.memory_source(),
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
