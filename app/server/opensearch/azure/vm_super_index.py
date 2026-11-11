"""Azure VM super index: the Azure virtual machine documents of every active tenant's
super_index_<tenant_id> index with a timestamp in the year (2026 by default), ordered by the
completed ingestion job that wrote them, newest first.

    GET super_index_<tenant_id>/_search
    {"query": {"bool": {"filter": [
        {"term": {"cloud_type.keyword": "AZURE"}},
        {"term": {"service_type.keyword": "microsoft.compute_virtualmachines"}},
        {"range": {"timestamp": {"gte": "2026-01-01", "lt": "2027-01-01"}}}
    ]}}}

Each document's job_id is matched to the completed data_ingestion_audit rows of the year
(app/server/mysql/completed_audits_latest.py); the paging, matching, ordering and progress
live in app/server/opensearch/super_index.py, shared with the AWS EC2 command. Read-only on
OpenSearch and on the company database.

Usage:
    python -m app.server.opensearch.azure.vm_super_index               # newest job first
    python -m app.server.opensearch.azure.vm_super_index --show 0      # list every document
    python -m app.server.opensearch.azure.vm_super_index --year 2025   # timestamps and audits of 2025
    python -m app.server.opensearch.azure.vm_super_index --json        # the ordered object
    python -m app.server.opensearch.azure.vm_super_index -q            # no progress lines

From code:
    from app.server.opensearch.azure.vm_super_index import fetch_vm_documents
    result = fetch_vm_documents()                # SuperIndexResult, result.jobs newest first
"""

from __future__ import annotations

import sys

from app.server.mysql.data_ingestion_audit import DEFAULT_STATUS, DEFAULT_YEAR
from app.server.mysql.ingestion_modules import DEFAULT_MODULE
from app.server.opensearch.super_index import SuperIndexResult, fetch_super_index, run_cli

CLOUD_TYPE = "AZURE"
SERVICE_TYPE = "microsoft.compute_virtualmachines"
TERMS = {"cloud_type.keyword": CLOUD_TYPE, "service_type.keyword": SERVICE_TYPE}


def fetch_vm_documents(
    year: int = DEFAULT_YEAR,
    audit_status: str = DEFAULT_STATUS,
    module_name: str = DEFAULT_MODULE,
) -> SuperIndexResult:
    """Azure VM super index documents of the active tenants, grouped per completed job, newest first."""
    return fetch_super_index(TERMS, year, audit_status, module_name)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit code 0 on success (even with no documents), 1 on failure."""
    return run_cli(
        "python -m app.server.opensearch.azure.vm_super_index",
        "Azure VM super index documents of the active tenants, newest completed job first (read-only).",
        TERMS,
        argv,
    )


if __name__ == "__main__":
    sys.exit(main())