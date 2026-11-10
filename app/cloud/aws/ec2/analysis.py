"""EC2 analysis runs: store instances, detect configuration changes, fill missing metrics,
report datapoint availability, cumulative utilization and evaluation readiness.

A run has six steps, reported one by one through an optional `on_step` callback:
    1. connect to AWS                   account and region
    2. find the instances               one DescribeInstances listing, every state
    3. instance details                 instance types (vCPU, memory) and EBS volumes
    4. configuration changes            compared with the stored configuration (first run: baseline)
    5. CloudWatch metrics               only hours not stored or checked yet are requested
    6. store and cumulative summaries   everything stored, cumulative statistics rebuilt

Windows come from app/config.py (calendar windows such as 30d = month to date, or rolling
ones such as 7d). An instance can only be evaluated once its current configuration covers the
whole window, i.e. the window holds no hour from before its launch or last significant change;
until then clients are told exactly when to run again (see app/cloud/windows.py).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import db
from app.auth import aws_auth
from app.auth.common import console, hint, status, warning
from app.cloud.aws.ec2 import store, utilization
from app.cloud.aws.ec2.changes import (
    InstanceChange,
    InstanceSnapshot,
    compare,
    instance_label,
    print_change,
    snapshot_instance,
)
from app.cloud.aws.ec2.configuration import InstanceConfiguration, build_configurations
from app.cloud.aws.ec2.instances import INSTANCE_STATES, describe_instances, state_summary
from app.cloud.aws.ec2.metrics import FetchResult, fetch, hours, plan_requests
from app.cloud.display import field, format_duration, format_time, plural, print_header, section
from app.cloud.windows import ANALYSIS_WINDOWS, DEFAULT_WINDOW, AnalysisWindow, Readiness, readiness_of
from app.db import resources

StepCallback = Callable[[str, str], None]
"""on_step(title, detail): called after each step completes."""

STEPS = (
    "Connect to AWS",
    "Find the instances",
    "Get instance details and configurations",
    "Check for configuration changes",
    "Collect CloudWatch metrics",
    "Store data and calculate cumulative averages",
)


@dataclass(frozen=True)
class AnalysisResult:
    """Everything one analysis run found."""

    run_id: int
    window: AnalysisWindow
    account_id: str
    region: str
    started_at: datetime
    previous_run_at: datetime | None
    baseline_created: bool
    configurations: dict[str, InstanceConfiguration]
    snapshots: dict[str, InstanceSnapshot]
    changes: list[InstanceChange]
    readiness: list[Readiness]
    fetched: FetchResult
    utilization: utilization.UtilizationReport
    cumulative: list[utilization.CumulativeResult]


@dataclass(frozen=True)
class StoredChange:
    """A change read back from the database, with the run that detected it."""

    run_id: int
    detected_at: datetime
    change: InstanceChange


def _stored_change(entry: resources.StoredResource | None) -> tuple[datetime | None, str]:
    """When a stored instance last changed, and whether that was it being added."""
    if entry is None or entry.last_changed_at is None:
        return None, "changed"
    return entry.last_changed_at, "added" if entry.last_changed_at == entry.first_seen_at else "changed"


def _restarted(changes: list[InstanceChange]) -> dict[str, str]:
    """Instances whose evaluation window restarts in this run, with the reason."""
    return {
        change.instance_id: "added" if change.kind == "added" else "changed"
        for change in changes
        if change.significant
    }


def _store_run(
    connection,
    instance_scope: resources.Scope,
    run_id: int,
    now: datetime,
    configs: dict[str, InstanceConfiguration],
    current: dict[str, InstanceSnapshot],
    stored: dict[str, resources.StoredResource],
    changes: list[InstanceChange],
) -> dict[str, store.MetricTarget]:
    """Store instances, snapshots and the detected changes; return each instance's metric target."""
    by_id = {change.instance_id: change for change in changes}
    restarted = _restarted(changes)

    targets: dict[str, store.MetricTarget] = {}
    for instance_id, snapshot in current.items():
        entry = stored.get(instance_id)
        resource, snapshot_id = store.save_instance(
            connection, instance_scope, run_id, now, configs[instance_id], snapshot,
            entry, changed=instance_id in restarted,
        )
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
    region: str | None = None,
    on_step: StepCallback = _ignore,
) -> AnalysisResult:
    """Run the six analysis steps (see module docstring) and return everything they found.

    The run is recorded in analysis_runs before steps 5 and 6, so a failure there is kept
    with its error.
    """
    window = ANALYSIS_WINDOWS[window_key]
    now = datetime.now(timezone.utc)
    start, end = window.bounds(now)

    # 1. Connect to AWS
    account_id = aws_auth.get_identity().account_id
    region = region or aws_auth.get_session().region_name
    on_step(STEPS[0], f"account {account_id} · region {region}")

    # 2. Find the instances
    raws = describe_instances(region, INSTANCE_STATES)
    states = Counter(raw["State"]["Name"] for raw in raws)
    on_step(STEPS[1], f"{plural(len(raws), 'instance')}" + (
        f" ({', '.join(f'{count} {state}' for state, count in states.most_common())})" if raws else ""
    ))

    # 3. Get instance details and configurations
    configs = {config.instance.instance_id: config for config in build_configurations(raws, region)}
    current = {instance_id: snapshot_instance(config) for instance_id, config in configs.items()}
    measured = sorted(iid for iid, snapshot in current.items() if snapshot.state != "terminated")
    types = {config.instance_type for config in configs.values()}
    volumes = sum(len(config.volumes) for config in configs.values())
    on_step(STEPS[2], f"{plural(len(types), 'instance type')} · {plural(volumes, 'EBS volume')}")

    # 4. Check for configuration changes
    with db.session() as connection:
        account_ref = resources.upsert_account(connection, store.PROVIDER, account_id, now)
        instance_scope = store.scope(account_ref, region)
        previous_run_at = resources.last_run_at(connection, instance_scope)
        stored = resources.load_resources(connection, instance_scope)
        coverage = store.load_coverage(connection, instance_scope, start, end, measured)
        run_id = resources.start_run(
            connection, instance_scope, window.key, int((end - start).total_seconds()), window.purpose, now
        )
    first_run = not stored
    active = {
        rid: InstanceSnapshot.from_dict(entry.tracked)
        for rid, entry in stored.items()
        if entry.removed_at is None and entry.tracked
    }
    changes = [] if first_run else compare(active, current)
    restarted = _restarted(changes)
    on_step(STEPS[3], "first run · baseline will be stored" if first_run else (
        f"{plural(len(changes), 'change')} since the last run" if changes else "no changes since the last run"
    ))

    try:
        # 5. Collect CloudWatch metrics (missing hours only)
        requests = plan_requests(measured, start, end, coverage)
        fetched = fetch(requests, region)
        on_step(STEPS[4], (
            f"{plural(fetched.api_calls, 'call')} · {plural(fetched.queries, 'metric query', 'metric queries')} · "
            f"{plural(fetched.datapoints, 'new datapoint')}"
            if requests else "no calls · every hour already stored"
        ))

        # 6. Store data and calculate cumulative averages
        with db.session() as connection:
            targets = _store_run(connection, instance_scope, run_id, now, configs, current, stored, changes)
            measured_targets = {iid: targets[iid] for iid in measured}
            utilization.save_fetched(connection, run_id, now, measured_targets, requests, fetched)
            report = utilization.build_report(
                connection, instance_scope, start, end, measured_targets, coverage, requests, fetched
            )
            utilization.record_report(connection, run_id, measured_targets, report)
            cumulative = utilization.update_cumulative(
                connection, instance_scope, run_id, now, measured_targets, current
            )
            resources.finish_run(
                connection, run_id, datetime.now(timezone.utc), len(current), len(changes), first_run
            )
        on_step(STEPS[5], f"run #{run_id} stored · "
                          f"{plural(len(cumulative), 'cumulative summary', 'cumulative summaries')}")
    except BaseException as exc:
        with suppress(Exception), db.session() as connection:
            resources.fail_run(connection, run_id, datetime.now(timezone.utc), f"{type(exc).__name__}: {exc}")
        raise

    last_changed = {
        iid: (now, restarted[iid]) if iid in restarted else _stored_change(stored.get(iid))
        for iid in current
    }
    readiness = [
        readiness_of(current[iid], configs[iid].launch_time, *last_changed[iid], window, now)
        for iid in measured
    ]
    readiness.sort(key=lambda item: (item.ready, item.snapshot.name.lower(), item.snapshot.instance_id))

    return AnalysisResult(
        run_id=run_id,
        window=window,
        account_id=account_id,
        region=region,
        started_at=now,
        previous_run_at=previous_run_at,
        baseline_created=first_run,
        configurations=configs,
        snapshots=current,
        changes=changes,
        readiness=readiness,
        fetched=fetched,
        utilization=report,
        cumulative=cumulative,
    )


def remaining(delta: timedelta) -> str:
    """Return e.g. '20 days 15 h', '5 h 10 min' or '12 min' (at least one minute)."""
    return format_duration(max(delta, timedelta(minutes=1)))


def _print_readiness(item: Readiness) -> None:
    label = instance_label(item.snapshot)
    if item.ready:
        detail = f"stable since {format_time(item.since)}" if item.since else "no recent launch or change"
        console.print(f"  [green]✔[/] {label}  [dim]ready · {detail}[/]")
        return
    console.print(
        f"  [yellow]⚠[/] {label}  [yellow]{item.reason} {format_time(item.since)} · "
        f"wait until {format_time(item.ready_at)} ({remaining(item.ready_at - item.checked_at)} left)[/]"
    )


def print_analysis(result: AnalysisResult) -> None:
    """Print the run summary, changes, datapoints, cumulative utilization and readiness."""
    window, report = result.window, result.utilization
    print_header("EC2 analysis", result.region, len(result.snapshots), state_summary(result.snapshots.values()))
    field("Window", window.label(report.start, report.end))
    span = len(hours(report.start, report.end))
    field("Period", f"{format_time(report.start)} → {format_time(report.end)} ({plural(span, 'hour')})")
    field("Account", result.account_id)
    field(
        "Run",
        f"#{result.run_id} · "
        + (f"previous run {format_time(result.previous_run_at)}" if result.previous_run_at else "first run"),
    )
    console.print()

    if result.baseline_created:
        status(True, f"Baseline stored for {len(result.snapshots)} instances (first run for this account and region)")
    elif not result.changes:
        status(True, "No configuration changes since the last run")
    else:
        warning(f"{len(result.changes)} configuration change(s) since the last run (stored for inspection)")
        for change in result.changes:
            print_change(change)
    console.print()

    utilization.print_report(report, statistics=False)
    utilization.print_cumulative(result.cumulative)

    section(f"Evaluation readiness ({window.key} window)", indent=0)
    if not result.readiness:
        console.print("  [dim]No instances to evaluate.[/]")
    for item in result.readiness:
        _print_readiness(item)
    console.print()

    waiting = [item for item in result.readiness if not item.ready]
    if not waiting:
        status(True, f"All instances are ready for a {window.key} evaluation")
        return

    latest = max(item.ready_at for item in waiting)
    warning(
        f"{len(waiting)} instance(s) were recently changed or launched; "
        f"their {window.key} results are not reliable yet"
    )
    hint((
        f"Wait until {format_time(latest)} ({remaining(latest - result.started_at)} from now), when the "
        f"{window.description} window no longer holds hours from before the change",
        "Run the analysis again then; changed instances are re-checked on every run",
    ))
    console.print()


def get_history(region: str | None = None, resource: str | None = None, limit: int = 20) -> list[StoredChange]:
    """Return stored changes for the account and region, newest first."""
    account_id = aws_auth.get_identity().account_id
    region = region or aws_auth.get_session().region_name
    now = datetime.now(timezone.utc)
    with db.session() as connection:
        account_ref = resources.upsert_account(connection, store.PROVIDER, account_id, now)
        records = resources.list_changes(connection, store.scope(account_ref, region), resource, limit)
    return [StoredChange(record.run_id, record.detected_at, store.to_instance_change(record)) for record in records]


def print_history(changes: list[StoredChange], region: str) -> None:
    """Print stored changes, newest first, each with when it was detected."""
    print_header("EC2 change history", region, len(changes))
    if not changes:
        console.print("  [dim]No changes stored.[/]")
        console.print()
        return

    for stored_change in changes:
        note = "" if stored_change.change.significant else " · [dim]does not restart evaluation[/]"
        console.print(f"[dim]{format_time(stored_change.detected_at)} · run #{stored_change.run_id}[/]{note}")
        print_change(stored_change.change)
        console.print()