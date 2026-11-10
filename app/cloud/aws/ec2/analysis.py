"""EC2 analysis runs: store instances, detect configuration changes, fill missing metrics,
report datapoint availability and evaluation readiness.

Every run stores each instance, its full configuration (a new snapshot whenever it changes),
tags, volumes and typed specs. The first run for an account and region creates the baseline;
later runs compare with the stored configuration and store each change.

Metrics: the window (30d = month to date, 90d = last 3 calendar months to date, testing windows
rolling) is checked against what is already stored, and only missing hours are requested from
CloudWatch. A run where nothing is missing makes no CloudWatch calls. Every run then rebuilds
the cumulative summary (cumulative average, percentiles, trend, volatility, ...) of each
instance's current configuration from all stored hours.

An instance can only be evaluated once a full window (30 days for 30d, ...) has passed since it
was launched or last changed; until then clients are told to wait.
"""

from __future__ import annotations

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
from app.cloud.aws.ec2.configuration import InstanceConfiguration, get_configurations
from app.cloud.aws.ec2.instances import INSTANCE_STATES, state_summary
from app.cloud.aws.ec2.metrics import fetch, hours, plan_requests
from app.cloud.aws.ec2.windows import ANALYSIS_WINDOWS, DEFAULT_WINDOW, AnalysisWindow
from app.cloud.display import field, format_time, plural, print_header, section
from app.db import resources


@dataclass(frozen=True)
class Readiness:
    """Whether an instance has been stable for a full window and can be evaluated."""

    snapshot: InstanceSnapshot
    reason: str
    since: datetime | None
    ready_at: datetime | None
    checked_at: datetime

    @property
    def ready(self) -> bool:
        return self.ready_at is None or self.checked_at >= self.ready_at


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
    snapshots: dict[str, InstanceSnapshot]
    changes: list[InstanceChange]
    readiness: list[Readiness]
    utilization: utilization.UtilizationReport
    cumulative: list[utilization.CumulativeResult]


@dataclass(frozen=True)
class StoredChange:
    """A change read back from the database, with the run that detected it."""

    run_id: int
    detected_at: datetime
    change: InstanceChange


def _readiness(
    snapshot: InstanceSnapshot,
    launched_at: datetime | None,
    changed_at: datetime | None,
    change_reason: str,
    window: AnalysisWindow,
    now: datetime,
) -> Readiness:
    """The evaluation window starts at the later of launch time and last change."""
    events = [(moment, reason) for moment, reason in ((launched_at, "launched"), (changed_at, change_reason)) if moment]
    if not events:
        return Readiness(snapshot, "stable", None, None, now)
    since, reason = max(events)
    return Readiness(snapshot, reason, since, since + window.duration, now)


def _stored_change(entry: resources.StoredResource | None) -> tuple[datetime | None, str]:
    """When a stored instance last changed, and whether that was it being added."""
    if entry is None or entry.last_changed_at is None:
        return None, "changed"
    return entry.last_changed_at, "added" if entry.last_changed_at == entry.first_seen_at else "changed"


def _store_run(
    connection,
    instance_scope: resources.Scope,
    run_id: int,
    now: datetime,
    configs: dict[str, InstanceConfiguration],
    current: dict[str, InstanceSnapshot],
    stored: dict[str, resources.StoredResource],
) -> tuple[bool, list[InstanceChange], dict[str, str], dict[str, store.MetricTarget]]:
    """Compare with the stored configuration, then store instances, snapshots and changes."""
    first_run = not stored
    active = {
        rid: InstanceSnapshot.from_dict(entry.tracked)
        for rid, entry in stored.items()
        if entry.removed_at is None and entry.tracked
    }
    changes = [] if first_run else compare(active, current)
    by_id = {change.instance_id: change for change in changes}
    restarted = {
        change.instance_id: "added" if change.kind == "added" else "changed"
        for change in changes
        if change.significant
    }

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

    return first_run, changes, restarted, targets


def run_analysis(window_key: str = DEFAULT_WINDOW, region: str | None = None) -> AnalysisResult:
    """Run one analysis: configuration, changes, missing metric hours, availability,
    cumulative summaries and readiness.

    The run is recorded first, so a failure is kept in analysis_runs with its error.
    """
    window = ANALYSIS_WINDOWS[window_key]
    now = datetime.now(timezone.utc)
    start, end = window.bounds(now)
    account_id = aws_auth.get_identity().account_id
    region = region or aws_auth.get_session().region_name

    configs = {config.instance.instance_id: config for config in get_configurations(region, states=INSTANCE_STATES)}
    current = {instance_id: snapshot_instance(config) for instance_id, config in configs.items()}
    measured = sorted(iid for iid, snapshot in current.items() if snapshot.state != "terminated")

    with db.session() as connection:
        account_ref = resources.upsert_account(connection, store.PROVIDER, account_id, now)
        instance_scope = store.scope(account_ref, region)
        previous_run_at = resources.last_run_at(connection, instance_scope)
        stored = resources.load_resources(connection, instance_scope)
        coverage = store.load_coverage(connection, instance_scope, start, end, measured)
        run_id = resources.start_run(
            connection, instance_scope, window.key, int((end - start).total_seconds()), window.purpose, now
        )

    try:
        requests = plan_requests(measured, start, end, coverage)
        fetched = fetch(requests, region)
        with db.session() as connection:
            first_run, changes, restarted, targets = _store_run(
                connection, instance_scope, run_id, now, configs, current, stored
            )
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
    except BaseException as exc:
        with suppress(Exception), db.session() as connection:
            resources.fail_run(connection, run_id, datetime.now(timezone.utc), f"{type(exc).__name__}: {exc}")
        raise

    last_changed = {
        iid: (now, restarted[iid]) if iid in restarted else _stored_change(stored.get(iid))
        for iid in current
    }
    readiness = [
        _readiness(current[iid], configs[iid].launch_time, *last_changed[iid], window, now)
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
        snapshots=current,
        changes=changes,
        readiness=readiness,
        utilization=report,
        cumulative=cumulative,
    )


def _remaining(delta: timedelta) -> str:
    """Return e.g. '29 days 23 h', '5 h 10 min' or '12 min'."""
    minutes = max(int(delta.total_seconds() // 60), 1)
    days, minutes = divmod(minutes, 24 * 60)
    hours_left, minutes = divmod(minutes, 60)
    if days:
        return f"{days} day{'s' if days != 1 else ''}" + (f" {hours_left} h" if hours_left else "")
    if hours_left:
        return f"{hours_left} h" + (f" {minutes} min" if minutes else "")
    return f"{minutes} min"


def _print_readiness(item: Readiness) -> None:
    label = instance_label(item.snapshot)
    if item.ready:
        detail = f"stable since {format_time(item.since)}" if item.since else "no recent launch or change"
        console.print(f"  [green]✔[/] {label}  [dim]ready · {detail}[/]")
        return
    console.print(
        f"  [yellow]⚠[/] {label}  [yellow]{item.reason} {format_time(item.since)} · "
        f"wait until {format_time(item.ready_at)} ({_remaining(item.ready_at - item.checked_at)} left)[/]"
    )


def print_analysis(result: AnalysisResult) -> None:
    """Print the run summary, changes, datapoints, cumulative utilization and readiness."""
    window, report = result.window, result.utilization
    print_header("EC2 analysis", result.region, len(result.snapshots), state_summary(result.snapshots.values()))
    field("Window", f"{window.key} · {window.purpose} · {window.description}")
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
        f"Wait {_remaining(window.duration)} after the change before evaluating them (until {format_time(latest)})",
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