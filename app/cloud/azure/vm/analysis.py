"""Azure VM analysis runs: store VMs, detect configuration changes, fill missing metrics,
report datapoint availability, cumulative utilization and evaluation readiness.

The same six steps as an EC2 run, reported one by one through an optional `on_step` callback:
    1. connect to Azure                 subscription, credential and location scope
    2. find the VMs                     one subscription-wide listing plus power states
    3. VM details                       VM size specs (vCPU, memory, family) and managed disks
    4. configuration changes            compared with the stored configuration (first run: baseline)
    5. Azure Monitor metrics            only hours not stored or checked yet are requested
    6. store and cumulative summaries   everything stored, cumulative statistics rebuilt

Windows and readiness rules are shared with EC2 (app/cloud/windows.py): a VM is evaluated
only once its current configuration covers the whole window.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import db
from app.auth import azure_auth
from app.auth.common import console, hint, status, warning
from app.cloud import utilization
from app.cloud.azure.vm import metrics, store
from app.cloud.azure.vm.changes import (
    VmChange,
    VmSnapshot,
    compare,
    configuration_version,
    print_change,
    snapshot_vm,
    vm_label,
)
from app.cloud.azure.vm.inventory import VmConfiguration, build_configurations, describe_vms, state_summary
from app.cloud.display import field, format_duration, format_time, plural, print_header, section
from app.cloud.timeseries import FetchResult, hours
from app.cloud.windows import ANALYSIS_WINDOWS, DEFAULT_WINDOW, AnalysisWindow, Readiness, readiness_of
from app.db import resources

StepCallback = Callable[[str, str], None]

STEPS = (
    "Connect to Azure",
    "Find the VMs",
    "Get VM details and configurations",
    "Check for configuration changes",
    "Collect Azure Monitor metrics",
    "Store data and calculate cumulative averages",
)
MONITOR = "Azure Monitor"


@dataclass(frozen=True)
class AnalysisResult:
    """Everything one Azure VM analysis run found."""

    run_id: int
    window: AnalysisWindow
    subscription_id: str
    subscription_name: str
    tenant_id: str
    location: str
    started_at: datetime
    previous_run_at: datetime | None
    baseline_created: bool
    configurations: dict[str, VmConfiguration]
    snapshots: dict[str, VmSnapshot]
    changes: list[VmChange]
    readiness: list[Readiness]
    fetched: FetchResult
    utilization: utilization.UtilizationReport
    cumulative: list[utilization.CumulativeResult]


def _stored_change(entry: resources.StoredResource | None) -> tuple[datetime | None, str]:
    if entry is None or entry.last_changed_at is None:
        return None, "changed"
    return entry.last_changed_at, "added" if entry.last_changed_at == entry.first_seen_at else "changed"


def _restarted(changes: list[VmChange]) -> dict[str, str]:
    """VMs whose evaluation window restarts in this run, with the reason."""
    return {change.instance_id: "added" if change.kind == "added" else "changed"
            for change in changes if change.significant}


def _store_run(connection, vm_scope: resources.Scope, run_id: int, now: datetime,
               configs: dict[str, VmConfiguration], current: dict[str, VmSnapshot],
               stored: dict[str, resources.StoredResource], changes: list[VmChange]) -> dict[str, store.MetricTarget]:
    """Store VMs, snapshots and the detected changes; return each VM's metric target."""
    by_id = {change.instance_id: change for change in changes}
    restarted = _restarted(changes)
    targets: dict[str, store.MetricTarget] = {}
    for instance_id, snapshot in current.items():
        entry = stored.get(instance_id)
        resource, snapshot_id = store.save_vm(connection, vm_scope, run_id, now, configs[instance_id], snapshot,
                                              entry, changed=instance_id in restarted)
        targets[instance_id] = store.MetricTarget(resource, snapshot.name)
        if change := by_id.get(instance_id):
            before_id = entry.current_snapshot_id if entry and change.kind == "modified" else None
            store.record_change(connection, resource, run_id, now, change, before_id, snapshot_id)
    for change in changes:
        if change.kind == "removed":
            entry = stored[change.instance_id]
            resources.mark_removed(connection, entry.ref, now)
            store.record_change(connection, entry.ref, run_id, now, change, entry.current_snapshot_id, None)
    return targets


def _ignore(title: str, detail: str) -> None:
    """Default step callback: report nothing."""


def run_analysis(
    window_key: str = DEFAULT_WINDOW,
    location: str | None = None,
    on_step: StepCallback = _ignore,
) -> AnalysisResult:
    """Run the six analysis steps (see module docstring) and return everything they found."""
    window = ANALYSIS_WINDOWS[window_key]
    now = datetime.now(timezone.utc)
    start, end = window.bounds(now)
    definitions = metrics.metrics()
    tables = store.tables(definitions)

    # 1. Connect to Azure
    identity = azure_auth.get_identity()
    on_step(STEPS[0], f"subscription {identity.name} · {azure_auth.credential_source()} · "
                      f"{location or 'all locations'}")

    # 2. Find the VMs
    records = describe_vms(location)
    states = Counter(raw["_state"] for raw in records)
    on_step(STEPS[1], plural(len(records), "VM") + (
        f" ({', '.join(f'{count} {state}' for state, count in states.most_common())})" if records else ""))

    # 3. Get VM details and configurations
    configs = {config.instance_id: config for config in build_configurations(records)}
    current = {instance_id: snapshot_vm(config) for instance_id, config in configs.items()}
    measured = sorted(current)
    sizes = {config.vm_size for config in configs.values()}
    disks = sum(len(config.disks) for config in configs.values())
    unknown = sorted(config.vm_size for config in configs.values() if config.size_spec is None)
    on_step(STEPS[2], f"{plural(len(sizes), 'VM size')} · {plural(disks, 'managed disk')}"
                      + (f" · no specs for {', '.join(unknown)}" if unknown else ""))

    # 4. Check for configuration changes
    with db.session() as connection:
        account_ref = resources.upsert_account(connection, store.PROVIDER, identity.subscription_id, now)
        vm_scope = store.scope(account_ref, location)
        previous_run_at = resources.last_run_at(connection, vm_scope)
        stored = resources.load_resources(connection, vm_scope)
        coverage = tables.load_coverage(connection, vm_scope, start, end, measured)
        run_id = resources.start_run(
            connection, vm_scope, window.key, int((end - start).total_seconds()), window.purpose, now
        )
    first_run = not stored
    active = {rid: VmSnapshot.from_dict(entry.tracked) for rid, entry in stored.items()
              if entry.removed_at is None and entry.tracked}
    changes = [] if first_run else compare(active, current)
    restarted = _restarted(changes)
    on_step(STEPS[3], "first run · baseline will be stored" if first_run else (
        f"{plural(len(changes), 'change')} since the last run" if changes else "no changes since the last run"))

    try:
        # 5. Collect Azure Monitor metrics (missing hours only)
        requests = metrics.plan_requests(measured, start, end, coverage)
        sources = {
            iid: metrics.MetricSource(configs[iid].vm.resource_id,
                                      configs[iid].size_spec.memory_gib if configs[iid].size_spec else None)
            for iid in measured
        }
        fetched = metrics.fetch(requests, sources)
        on_step(STEPS[4], (
            f"{plural(fetched.api_calls, 'call')} · {plural(fetched.queries, 'metric query', 'metric queries')} · "
            f"{plural(fetched.datapoints, 'new datapoint')} · memory from "
            f"{metrics.MEMORY_SOURCE_LABELS[metrics.memory_source()]}"
            if requests else "no calls · every hour already stored"
        ))

        # 6. Store data and calculate cumulative averages
        with db.session() as connection:
            targets = _store_run(connection, vm_scope, run_id, now, configs, current, stored, changes)
            utilization.save_fetched(tables, connection, run_id, now, targets, requests, fetched)
            report = utilization.build_report(tables, connection, vm_scope, start, end, targets, coverage,
                                              requests, fetched)
            utilization.record_report(tables, connection, run_id, targets, report)
            cumulative = utilization.update_cumulative(
                tables, connection, vm_scope, run_id, now, targets,
                {iid: configuration_version(snapshot) for iid, snapshot in current.items()},
            )
            resources.finish_run(connection, run_id, datetime.now(timezone.utc), len(current), len(changes),
                                 first_run)
        on_step(STEPS[5], f"run #{run_id} stored · "
                          f"{plural(len(cumulative), 'cumulative summary', 'cumulative summaries')}")
    except BaseException as exc:
        with suppress(Exception), db.session() as connection:
            resources.fail_run(connection, run_id, datetime.now(timezone.utc), f"{type(exc).__name__}: {exc}")
        raise

    last_changed = {iid: (now, restarted[iid]) if iid in restarted else _stored_change(stored.get(iid))
                    for iid in current}
    ready = [readiness_of(current[iid], configs[iid].time_created, *last_changed[iid], window, now) for iid in measured]
    ready.sort(key=lambda item: (item.ready, item.snapshot.name.lower(), item.snapshot.instance_id))

    return AnalysisResult(
        run_id=run_id, window=window, subscription_id=identity.subscription_id, subscription_name=identity.name,
        tenant_id=identity.tenant_id, location=location or store.ALL_LOCATIONS, started_at=now,
        previous_run_at=previous_run_at, baseline_created=first_run, configurations=configs, snapshots=current,
        changes=changes, readiness=ready, fetched=fetched, utilization=report, cumulative=cumulative,
    )


def remaining(delta: timedelta) -> str:
    return format_duration(max(delta, timedelta(minutes=1)))


def _print_readiness(item: Readiness) -> None:
    label = vm_label(item.snapshot)
    if item.ready:
        detail = f"stable since {format_time(item.since)}" if item.since else "no recent creation or change"
        console.print(f"  [green]✔[/] {label}  [dim]ready · {detail}[/]")
        return
    console.print(
        f"  [yellow]⚠[/] {label}  [yellow]{item.reason} {format_time(item.since)} · "
        f"wait until {format_time(item.ready_at)} ({remaining(item.ready_at - item.checked_at)} left)[/]"
    )


def print_analysis(result: AnalysisResult) -> None:
    """Print the run summary, changes, datapoints, cumulative utilization and readiness."""
    window, report = result.window, result.utilization
    scope = f"{result.subscription_name} · {result.location}"
    print_header("Azure VM analysis", scope, len(result.snapshots), state_summary(c.vm for c in result.configurations.values()))
    field("Window", window.label(report.start, report.end))
    span = len(hours(report.start, report.end))
    field("Period", f"{format_time(report.start)} → {format_time(report.end)} ({plural(span, 'hour')})")
    field("Subscription", result.subscription_id)
    memory = metrics.metrics()[1]
    field("Memory source", f"{metrics.MEMORY_SOURCE_LABELS[metrics.memory_source()]} · {memory.metric_name}")
    field("Run", f"#{result.run_id} · " + (
        f"previous run {format_time(result.previous_run_at)}" if result.previous_run_at else "first run"))
    console.print()

    if result.baseline_created:
        status(True, f"Baseline stored for {len(result.snapshots)} VMs (first run for this subscription and scope)")
    elif not result.changes:
        status(True, "No configuration changes since the last run")
    else:
        warning(f"{len(result.changes)} configuration change(s) since the last run (stored for inspection)")
        for change in result.changes:
            print_change(change)
    console.print()

    utilization.print_report(report, metrics.metrics(), MONITOR, statistics=False)
    utilization.print_cumulative(result.cumulative)

    section(f"Evaluation readiness ({window.key} window)", indent=0)
    if not result.readiness:
        console.print("  [dim]No VMs to evaluate.[/]")
    for item in result.readiness:
        _print_readiness(item)
    console.print()

    waiting = [item for item in result.readiness if not item.ready]
    if not waiting:
        status(True, f"All VMs are ready for a {window.key} evaluation")
        return
    latest = max(item.ready_at for item in waiting)
    warning(f"{len(waiting)} VM(s) were recently changed or created; their {window.key} results are not reliable yet")
    hint((
        f"Wait until {format_time(latest)} ({remaining(latest - result.started_at)} from now), when the "
        f"{window.description} window no longer holds hours from before the change",
        "Run the analysis again then; changed VMs are re-checked on every run",
    ))
    console.print()
