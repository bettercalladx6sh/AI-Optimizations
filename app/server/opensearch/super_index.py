"""Super index: the documents of the per-tenant super_index_<tenant_id> OpenSearch indices,
ordered by the ingestion run (job) that wrote them, most recently completed run first.

    1. completed audits   data_ingestion_audit rows of the active tenants (super_index module,
                          audit_date in the year, status COMPLETED), newest first
                          (app/server/mysql/completed_audits_latest.py)
    2. documents          GET super_index_<tenant_id>/_search for every active tenant, with a
                          bool filter of term clauses (e.g. cloud_type.keyword = AWS) and a
                          timestamp range for the year (2026-01-01 <= timestamp < 2027-01-01),
                          every page read through the scroll API
    3. ordering           each document joins the audit with the same tenant_id and job_id;
                          jobs are listed in audit order (newest first), and the documents of
                          a job by timestamp, newest first

While it runs, a live progress line per step is drawn on stderr (spinner, bar, documents
fetched of the total OpenSearch reports, elapsed time), so a slow read is visible as it moves
and a stuck one shows a clock that keeps running on the same count. stdout stays clean for
--json; -q turns the progress off.

A command can also save the result as a JSON file (run_cli(..., output=...), e.g. the Azure
VM command writes opensearch_results/azure_opensearch.json): an array of the matched hits
exactly as OpenSearch returned them (_index, _id, _score, _source), in the same order as the
output (newest completed job first, then newest timestamp). Every run replaces that same file
in place (atomically, via app/cloud/files.write_json); it never creates numbered copies.

Only documents whose timestamp falls in the year are kept: OpenSearch filters on the range,
and each returned document is checked again (its timestamp must start with "2026-"), so a
document from another year never reaches the output even if the field is not mapped as a date.
Documents whose job_id matches no completed audit of the year are counted per tenant and job
but left out of the ordered jobs. A tenant without a super_index index is reported, not an
error. Everything here only reads: _search and _search/scroll (the scroll context is cleared
when the read ends) on OpenSearch, SELECT on the company database.

The cloud specific commands (e.g. app/server/opensearch/aws/ec2/ec2_super_index.py) only name
their term filters and call run_cli().
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from opensearchpy.exceptions import NotFoundError, TransportError
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn

from app.auth import opensearch_auth
from app.auth.common import AuthError, console, hint, status
from app.cloud.display import NOT_SET, plural
from app.cloud.files import PROJECT_ROOT, display_path, write_json
from app.server.mysql.active_tenants import Tenant
from app.server.mysql.completed_audits_latest import newest_first
from app.server.mysql.data_ingestion_audit import DEFAULT_STATUS, DEFAULT_YEAR, Audit, AuditResult, fetch_audits
from app.server.mysql.ingestion_modules import DEFAULT_MODULE

INDEX_PREFIX = "super_index_"
PAGE_SIZE = 1000
SCROLL_KEEP_ALIVE = "2m"
SEARCH_TIMEOUT = 60
TIMESTAMP_FIELD = "timestamp"
SCROLL_PARAMS = {"scroll": SCROLL_KEEP_ALIVE, "request_timeout": SEARCH_TIMEOUT}
DEFAULT_SHOWN = 5
RESULTS_DIR = PROJECT_ROOT / "opensearch_results"

Document = dict[str, Any]
Hit = dict[str, Any]                                  # one search hit: _index, _id, _score, _source
stderr = Console(stderr=True)                         # progress and file notes; stdout stays for --json
PageCallback = Callable[[int, "int | None"], None]   # (documents fetched so far, total or None)


def index_name(tenant_id: str) -> str:
    """The super index of one tenant, e.g. super_index_d3fef9bc-...."""
    return f"{INDEX_PREFIX}{tenant_id}"


def year_range(year: int) -> dict[str, Any]:
    """Range clause: timestamp from 1 January of the year up to (not including) the next year."""
    return {"range": {TIMESTAMP_FIELD: {"gte": f"{year}-01-01", "lt": f"{year + 1}-01-01"}}}


def filter_query(terms: dict[str, str], year: int) -> dict[str, Any]:
    """{"query": {"bool": {"filter": [{"term": {field: value}}, ..., {"range": {"timestamp": ...}}]}}}"""
    clauses = [{"term": {name: value}} for name, value in terms.items()]
    return {"query": {"bool": {"filter": [*clauses, year_range(year)]}}}


def _in_year(document: Document, year: int) -> bool:
    """True when the document's timestamp is in the year, e.g. '2026-08-01T03:46:45+00:00'."""
    return str(document.get(TIMESTAMP_FIELD) or "").startswith(f"{year}-")


def _total_hits(page: dict[str, Any]) -> int | None:
    """hits.total of a search response ({"value": n} or n), None when absent."""
    total = page.get("hits", {}).get("total")
    if isinstance(total, dict):
        total = total.get("value")
    return total if isinstance(total, int) else None


def search_all(index: str, query: dict[str, Any], on_page: PageCallback | None = None) -> list[dict[str, Any]] | None:
    """Every hit of the query in the index (all pages), or None when the index does not exist.
    on_page(fetched, total) is called after every page, for progress."""
    client = opensearch_auth.get_client()
    body = {**query, "size": PAGE_SIZE, "track_total_hits": True}
    try:
        page = client.search(index=index, body=body, params=SCROLL_PARAMS)
    except NotFoundError:
        return None
    except TransportError as exc:
        raise opensearch_auth._to_auth_error(exc) from exc

    hits: list[dict[str, Any]] = []
    total = _total_hits(page)
    scroll_id = page.get("_scroll_id")
    try:
        while True:
            batch = page["hits"]["hits"]
            hits.extend(batch)
            if on_page:
                on_page(len(hits), total)
            if len(batch) < PAGE_SIZE or not scroll_id:
                return hits
            page = client.scroll(scroll_id=scroll_id, params=SCROLL_PARAMS)
            scroll_id = page.get("_scroll_id") or scroll_id
    except TransportError as exc:
        raise opensearch_auth._to_auth_error(exc) from exc
    finally:
        if scroll_id:
            try:
                client.clear_scroll(scroll_id=scroll_id)
            except TransportError:
                pass  # the context expires on its own after SCROLL_KEEP_ALIVE


class FetchProgress:
    """Live progress on stderr: one line per step with a spinner, a bar, the count fetched of
    the total and the elapsed time. Disabled, every call does nothing."""

    def __init__(self, enabled: bool = True) -> None:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("[dim]{task.fields[unit]}[/]"),
            TimeElapsedColumn(),
            console=stderr,
        ) if enabled else None

    def __enter__(self) -> FetchProgress:
        if self._progress:
            self._progress.start()
        return self

    def __exit__(self, *_: object) -> None:
        if self._progress:
            self._progress.stop()

    def start(self, description: str, unit: str = "documents") -> TaskID | None:
        """Add a step line; its count stays '?' until a total is known."""
        return self._progress.add_task(escape(description), total=None, unit=unit) if self._progress else None

    def update(self, task: TaskID | None, completed: int, total: int | None) -> None:
        if self._progress and task is not None:
            self._progress.update(task, completed=completed, total=total)

    def finish(self, task: TaskID | None, completed: int, note: str = "") -> None:
        """Fill the step's bar and optionally append a note, e.g. 'no index'."""
        if self._progress and task is not None:
            description = self._progress.tasks[task].description
            self._progress.update(task, completed=completed, total=completed,
                                  description=f"{description} [dim]· {escape(note)}[/]" if note else description)


def _document(hit: Hit) -> Document:
    """A hit as one flat object: its _id as doc_id, then every _source field unchanged."""
    return {"doc_id": hit.get("_id"), **hit.get("_source", {})}


def _newest_hit_first(hits: list[Hit]) -> list[Hit]:
    def key(hit: Hit) -> tuple[str, str]:
        source = hit.get("_source", {})
        return str(source.get(TIMESTAMP_FIELD) or ""), str(source.get("resource_id") or "")
    return sorted(hits, key=key, reverse=True)


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass(frozen=True)
class JobDocuments:
    """The documents one completed ingestion job wrote to a tenant's super index."""

    rank: int
    tenant: Tenant
    audit: Audit
    index: str
    hits: list[Hit]                          # as OpenSearch returned them, newest timestamp first

    @property
    def documents(self) -> list[Document]:
        return [_document(hit) for hit in self.hits]

    def to_dict(self) -> dict[str, Any]:
        audit = self.audit
        return {
            "rank": self.rank,
            "tenant_id": self.tenant.tenant_id,
            "company_name": self.tenant.company_name,
            "job_id": audit.job_id,
            "audit_id": audit.id,
            "audit_date": _iso(audit.audit_date),
            "start_time": _iso(audit.start_time),
            "end_time": _iso(audit.end_time),
            "index": self.index,
            "document_count": len(self.documents),
            "documents": self.documents,
        }


@dataclass(frozen=True)
class SuperIndexResult:
    """The filter, the ordered jobs and what could not be matched."""

    terms: dict[str, str]
    audits: AuditResult
    jobs: list[JobDocuments]                 # most recently completed job first
    unmatched: dict[str, Counter[str]]       # tenant_id -> job_id -> documents without a completed audit
    missing_indices: list[str]
    total: int                               # documents of the year found, matched or not
    outside_year: int                        # returned by OpenSearch but timestamped in another year

    @property
    def matched(self) -> int:
        return sum(len(job.hits) for job in self.jobs)

    def ordered_hits(self) -> list[Hit]:
        """Every matched hit as OpenSearch returned it, newest completed job first."""
        return [hit for job in self.jobs for hit in job.hits]

    def to_dict(self) -> dict[str, Any]:
        return {
            "filter": self.terms,
            "module": self.audits.module.module_name,
            "year": self.audits.year,
            "status": self.audits.status,
            "tenants": [{"tenant_id": t.tenant_id, "company_name": t.company_name} for t in self.audits.tenants],
            "summary": {
                "documents": self.total,
                "matched": self.matched,
                "unmatched": self.total - self.matched,
                "outside_year": self.outside_year,
                "jobs": len(self.jobs),
                "completed_audits": len(self.audits.audits),
            },
            "jobs": [job.to_dict() for job in self.jobs],
            "unmatched": {tenant_id: dict(jobs) for tenant_id, jobs in self.unmatched.items()},
            "missing_indices": self.missing_indices,
        }


def fetch_super_index(
    terms: dict[str, str],
    year: int = DEFAULT_YEAR,
    audit_status: str = DEFAULT_STATUS,
    module_name: str = DEFAULT_MODULE,
    progress: FetchProgress | None = None,
) -> SuperIndexResult:
    """Super index documents of every active tenant matching the terms, grouped per completed
    job and ordered from the most recently completed job to the oldest (read-only)."""
    progress = progress or FetchProgress(enabled=False)
    step = progress.start("Completed audits (company database)", unit="rows")
    audits = fetch_audits(year, audit_status, module_name)
    progress.finish(step, len(audits.audits))
    ordered = newest_first(audits.audits)
    by_job: dict[tuple[str, str], Audit] = {}
    for audit in ordered:
        if audit.job_id:
            by_job.setdefault((audit.tenant_id, audit.job_id), audit)  # newest audit wins a repeated job

    query = filter_query(terms, year)
    grouped: dict[tuple[str, str], list[Hit]] = {}
    unmatched: dict[str, Counter[str]] = {}
    missing: list[str] = []
    total = outside_year = 0
    for number, tenant in enumerate(audits.tenants, 1):
        index = index_name(tenant.tenant_id)
        step = progress.start(f"{tenant.company_name or tenant.tenant_id} ({number}/{len(audits.tenants)})")
        hits = search_all(index, query, lambda fetched, found, step=step: progress.update(step, fetched, found))
        if hits is None:
            progress.finish(step, 0, "no index")
            missing.append(index)
            continue
        progress.finish(step, len(hits))
        for hit in hits:
            source = hit.get("_source", {})
            if not _in_year(source, year):
                outside_year += 1
                continue
            total += 1
            key = (tenant.tenant_id, str(source.get("job_id") or ""))
            if key in by_job:
                grouped.setdefault(key, []).append(hit)
            else:
                unmatched.setdefault(tenant.tenant_id, Counter())[key[1] or NOT_SET] += 1

    tenants = {tenant.tenant_id: tenant for tenant in audits.tenants}
    jobs: list[JobDocuments] = []
    for key, audit in by_job.items():  # insertion order = newest first
        hits = grouped.get(key)
        if hits:
            jobs.append(JobDocuments(len(jobs) + 1, tenants[audit.tenant_id], audit,
                                     index_name(audit.tenant_id), _newest_hit_first(hits)))
    return SuperIndexResult(terms, audits, jobs, unmatched, missing, total, outside_year)


# ---------------------------------------------------------------- output


def _time(moment: datetime | None) -> str:
    return f"{moment:%Y-%m-%d %H:%M:%S}" if moment else NOT_SET


def _cost(value: object) -> str:
    return f"{value:,.2f}" if isinstance(value, (int, float)) else NOT_SET


def _text(value: object) -> str:
    return NOT_SET if value in (None, "", "null") else str(value)


def print_result(result: SuperIndexResult, shown: int = DEFAULT_SHOWN) -> None:
    filters = " · ".join(escape(value) for value in result.terms.values())
    year = result.audits.year
    console.print()
    console.print(f"[bold]Super index[/] [dim]·[/] {filters} [dim]·[/] {year} [dim]·[/] "
                  f"{escape(result.audits.status)} [dim]·[/] {plural(result.matched, 'document')} in "
                  f"{plural(len(result.jobs), 'job')} [dim]({result.total} found in "
                  f"{plural(len(result.audits.tenants), 'active tenant')}, timestamp in {year})[/]", highlight=False)
    console.print()

    width = max((len(job.tenant.company_name) for job in result.jobs), default=0)
    for job in result.jobs:
        audit = job.audit
        console.print(
            f"  [dim]{job.rank:>4}[/]  {str(audit.audit_date or NOT_SET):<10}  "
            f"[bold]{escape(job.tenant.company_name):<{width}}[/]  {plural(len(job.documents), 'document')}  "
            f"[dim]completed {_time(audit.end_time)} · job {audit.job_id}[/]",
            highlight=False,
        )
        listed = job.documents if shown == 0 else job.documents[:shown]
        name_width = max((len(_text(doc.get("resource_name"))) for doc in listed), default=0)
        region_width = max((len(_text(doc.get("region"))) for doc in listed), default=0)
        for doc in listed:
            console.print(
                f"          {escape(_text(doc.get('resource_name'))):<{name_width}}  "
                f"{escape(_text(doc.get('region'))):<{region_width}}  "
                f"[dim]mtd {_cost(doc.get('mtd_cost'))} · forecast {_cost(doc.get('mtd_forecast'))} · "
                f"{escape(_text(doc.get('account_name')))} · {escape(_text(doc.get('resource_id')))}[/]",
                highlight=False,
            )
        if len(job.documents) > len(listed):
            console.print(f"          [dim]… {len(job.documents) - len(listed)} more[/]")
    if result.jobs:
        console.print()

    names = {tenant.tenant_id: tenant.company_name for tenant in result.audits.tenants}
    for tenant_id, jobs in result.unmatched.items():
        console.print(f"  [yellow]●[/] {escape(names.get(tenant_id, tenant_id))} [dim]· "
                      f"{plural(sum(jobs.values()), 'document')} in {plural(len(jobs), 'job')} without a "
                      f"completed {year} audit[/]", highlight=False)
    if result.outside_year:
        console.print(f"  [yellow]●[/] [dim]{plural(result.outside_year, 'document')} skipped: "
                      f"timestamp not in {year}[/]", highlight=False)
    for index in result.missing_indices:
        console.print(f"  [yellow]●[/] [dim]no index {escape(index)}[/]", highlight=False)
    if result.unmatched or result.outside_year or result.missing_indices:
        console.print()


def _not_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def save_hits(result: SuperIndexResult, path: Path) -> None:
    """Write the ordered hits to the JSON file, replacing it in place, and say so on stderr."""
    hits = result.ordered_hits()
    replaced = write_json(hits, path)
    stderr.print(f"[bold green]✔[/] {'Updated' if replaced else 'Created'} {escape(display_path(path))} "
                 f"[dim]· {plural(len(hits), 'hit')}[/]", highlight=False)


def run_cli(
    prog: str,
    description: str,
    terms: dict[str, str],
    argv: list[str] | None = None,
    output: Path | None = None,
) -> int:
    """Shared CLI for the cloud specific commands: exit code 0 on success, 1 on failure.
    With an output path, the ordered hits are also saved there (-o changes the path)."""
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR, help=f"year of audit_date and document timestamp (default: {DEFAULT_YEAR})")
    parser.add_argument("--status", default=DEFAULT_STATUS, help=f"audit status (default: {DEFAULT_STATUS})")
    parser.add_argument("--module", default=DEFAULT_MODULE, help=f"ingestion module name (default: {DEFAULT_MODULE})")
    parser.add_argument("--show", type=_not_negative, default=DEFAULT_SHOWN, metavar="COUNT",
                        help=f"documents listed per job, 0 for all (default: {DEFAULT_SHOWN})")
    parser.add_argument("-j", "--json", action="store_true", help="print the ordered object as JSON")
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress lines while fetching")
    if output:
        parser.add_argument("-o", "--output", type=Path, default=output, metavar="PATH",
                            help=f"JSON file of the ordered hits, replaced on every run (default: {display_path(output)})")
    args = parser.parse_args(argv)

    try:
        with FetchProgress(enabled=not args.quiet) as progress:
            result = fetch_super_index(terms, args.year, args.status, args.module, progress)
    except AuthError as exc:
        status(False, f"Could not read the super index: {exc}")
        hint(exc.hints)
        return 1

    if output:
        try:
            save_hits(result, args.output)
        except OSError as exc:
            status(False, f"Could not write {args.output}: {exc}")
            return 1

    if args.json:
        print(json.dumps(result.to_dict(), default=str))
    else:
        print_result(result, args.show)
    return 0