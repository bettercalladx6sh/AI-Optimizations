"""EC2 super index: the AWS EC2 documents of every active tenant's super_index_<tenant_id>
index with a timestamp in the year (2026 by default), ordered by the completed ingestion job
that wrote them, newest first.

    GET super_index_<tenant_id>/_search
    {"query": {"bool": {"filter": [
        {"term": {"cloud_type.keyword": "AWS"}},
        {"term": {"product_code.keyword": "AmazonEC2"}},
        {"range": {"timestamp": {"gte": "2026-01-01", "lt": "2027-01-01"}}}
    ]}}}

Each document's job_id is matched to the completed data_ingestion_audit rows of the year
(app/server/mysql/completed_audits_latest.py); the paging, matching and ordering live in
app/server/opensearch/super_index.py. Read-only on OpenSearch and on the company database.

Every run also saves the matched hits, exactly as OpenSearch returned them (_index, _id,
_score, _source) and in the same newest-first order, to opensearch_results/aws_ec2_opensearch.json.
The same file is replaced on each run; -o writes somewhere else.

Usage:
    python -m app.server.opensearch.aws.ec2.ec2_super_index               # newest job first
    python -m app.server.opensearch.aws.ec2.ec2_super_index --show 0      # list every document
    python -m app.server.opensearch.aws.ec2.ec2_super_index --year 2025   # timestamps and audits of 2025
    python -m app.server.opensearch.aws.ec2.ec2_super_index --json        # the ordered object
    python -m app.server.opensearch.aws.ec2.ec2_super_index -q            # no progress lines
    python -m app.server.opensearch.aws.ec2.ec2_super_index -o ec2.json   # save the hits to another file

From code:
    from app.server.opensearch.aws.ec2.ec2_super_index import fetch_ec2_documents
    result = fetch_ec2_documents()               # SuperIndexResult, result.jobs newest first
"""

from __future__ import annotations

import sys

from app.server.mysql.data_ingestion_audit import DEFAULT_STATUS, DEFAULT_YEAR
from app.server.mysql.ingestion_modules import DEFAULT_MODULE
from app.server.opensearch.super_index import RESULTS_DIR, SuperIndexResult, fetch_super_index, run_cli

CLOUD_TYPE = "AWS"
PRODUCT_CODE = "AmazonEC2"
TERMS = {"cloud_type.keyword": CLOUD_TYPE, "product_code.keyword": PRODUCT_CODE}
OUTPUT_PATH = RESULTS_DIR / "aws_ec2_opensearch.json"


def fetch_ec2_documents(
    year: int = DEFAULT_YEAR,
    audit_status: str = DEFAULT_STATUS,
    module_name: str = DEFAULT_MODULE,
) -> SuperIndexResult:
    """AWS EC2 super index documents of the active tenants, grouped per completed job, newest first."""
    return fetch_super_index(TERMS, year, audit_status, module_name)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit code 0 on success (even with no documents), 1 on failure."""
    return run_cli(
        "python -m app.server.opensearch.aws.ec2.ec2_super_index",
        "AWS EC2 super index documents of the active tenants, newest completed job first (read-only).",
        TERMS,
        argv,
        output=OUTPUT_PATH,
    )


if __name__ == "__main__":
    sys.exit(main())