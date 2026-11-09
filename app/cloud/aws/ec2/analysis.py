"""EC2 analysis runs: store instances, detect configuration changes, report evaluation readiness.

Every run stores each instance, its full configuration (a new snapshot whenever it changes),
its tags, volumes and typed specs, and its hourly CPU and memory averages from CloudWatch for
the whole window in the analysis database. The first run for an account and region creates
the baseline; every later run compares with the stored configuration and stores each change
for later inspection.

An instance can only be evaluated once a full analysis window has passed since it was
launched or last changed; until then clients are told to wait.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from rich.markup import escape

from app import db
from app.auth import aws_auth
from app.auth.common import console, hint, status, warning
from app.cloud.aws.ec2 import store
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
from app.cloud.aws.ec2.metrics import (
    METRICS,
    MetricSeries,
    by_instance,
    collect_metrics,
    collection_window,
    print_series_summary,
)
from app.cloud.display import field, print_header, section
from app.db import resources


@dataclass(frozen=True)
class AnalysisWindow:
    """How far back an analysis looks, and whether it is a production or testing run."""

    key: str
    duration: timedelta
    purpose: str


# Analysis windows: production uses 30d / 90d, testing uses the short ones.
ANALYSIS_WINDOWS = {
    window.key: window
    for window in (
        AnalysisWindow("1h", timedelta(hours=1), "testing"),
        AnalysisWindow("1d", timedelta(days=1), "testing"),
        AnalysisWindow("3d", timedelta(days=3), "testing"),
        AnalysisWindow("7d", timedelta(days=7), "testing"),
        AnalysisWindow("30d", timedelta(days=30), "production"),
        AnalysisWindow("90d", timedelta(days=90), "production"),
    )
}
DEFAULT_WINDOW = "30d"


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
    metrics: dict[str, list[MetricSeries]]


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


def _store_run(
    connection,
    instance_scope: resources.Scope,
    run_id: int,
    now: datetime,
    configs: dict[str, InstanceConfiguration],
    current: dict[str, InstanceSnapshot],
    metrics: dict[str, list[MetricSeries]],
) -> tuple[bool, list[InstanceChange], dict[str, resources.StoredResource], dict[str, str]]:
    """Compare with the stored configuration, then store instances, snapshots, changes and metrics."""
    stored = resources.load_resources(connection, instance_scope)
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

    for instance_id, snapshot in current.items():
        entry = stored.get(instance_id)
        resource, snapshot_id = store.save_instance(
            connection, instance_scope, run_id, now, configs[instance_id], snapshot,
            entry, changed=instance_id in restarted,
        )
        if change := by_id.get(instance_id):
            before_id = entry.current_snapshot_id if entry and change.kind == "modified" else None
            store.record_change(connection, resource, run_id, now, change, before_id, snapshot_id)
        if instance_id in metrics:
            store.save_metrics(connection, resource, run_id, now, metrics[instance_id])

    for change in changes:
        if change.kind == "removed":
            entry = stored[change.instance_id]
            resources.mark_removed(connection, entry.ref, now)
            store.record_change(connection, entry.ref, run_id, now, change, entry.current_snapshot_id, None)

    resources.finish_run(connection, run_id, datetime.now(timezone.utc), len(current), len(changes), first_run)
    return first_run, changes, stored, restarted


def run_analysis(window_key: str = DEFAULT_WINDOW, region: str | None = None) -> AnalysisResult:
    """Run one analysis: collect configuration and metrics, store everything, detect changes,
    compute readiness.

    The run is recorded first, so a failure is kept in analysis_runs with its error.
    """
    window = ANALYSIS_WINDOWS[window_key]
    now = datetime.now(timezone.utc)
    account_id = aws_auth.get_identity().account_id
    region = region or aws_auth.get_session().region_name

    configs = {config.instance.instance_id: config for config in get_configurations(region, states=INSTANCE_STATES)}
    current = {instance_id: snapshot_instance(config) for instance_id, config in configs.items()}
    metrics = by_instance(collect_metrics(
        (iid for iid, snapshot in current.items() if snapshot.state != "terminated"),
        window.duration,
        region,
        now,
    ))

    with db.session() as connection:
        account_ref = resources.upsert_account(connection, store.PROVIDER, account_id, now)
        instance_scope = store.scope(account_ref, region)
        previous_run_at = resources.last_run_at(connection, instance_scope)
        run_id = resources.start_run(
            connection, instance_scope, window.key, int(window.duration.total_seconds()), window.purpose, now
        )

    try:
        with db.session() as connection:
            first_run, changes, stored, restarted = _store_run(
                connection, instance_scope, run_id, now, configs, current, metrics
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
        _readiness(snapshot, configs[iid].launch_time, *last_changed[iid], window, now)
        for iid, snapshot in current.items()
        if snapshot.state != "terminated"
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
        metrics=metrics,
    )


def _when(moment: datetime | None) -> str:
    return f"{moment:%Y-%m-%d %H:%M} UTC" if moment else ""


def _remaining(delta: timedelta) -> str:
    """Return e.g. '29 days 23 h', '5 h 10 min' or '12 min'."""
    minutes = max(int(delta.total_seconds() // 60), 1)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days} day{'s' if days != 1 else ''}" + (f" {hours} h" if hours else "")
    if hours:
        return f"{hours} h" + (f" {minutes} min" if minutes else "")
    return f"{minutes} min"


def _print_readiness(item: Readiness) -> None:
    label = instance_label(item.snapshot)
    if item.ready:
        detail = f"stable since {_when(item.since)}" if item.since else "no recent launch or change"
        console.print(f"  [green]✔[/] {label}  [dim]ready · {detail}[/]")
        return
    console.print(
        f"  [yellow]⚠[/] {label}  [yellow]{item.reason} {_when(item.since)} · "
        f"wait until {_when(item.ready_at)} ({_remaining(item.ready_at - item.checked_at)} left)[/]"
    )


def print_analysis(result: AnalysisResult) -> None:
    """Print the run summary, the changes since the last run and each instance's readiness."""
    window = result.window
    print_header("EC2 analysis", result.region, len(result.snapshots), state_summary(result.snapshots.values()))
    field("Window", f"{window.key} · {window.purpose}")
    field("Account", result.account_id)
    field(
        "Run",
        f"#{result.run_id} · "
        + (f"previous run {_when(result.previous_run_at)}" if result.previous_run_at else "first run"),
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

    print_utilization(result.metrics, result.snapshots, window)

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
        f"Wait a full {window.key} window after the change before evaluating them (until {_when(latest)})",
        "Run the analysis again then; changed instances are re-checked on every run",
    ))
    console.print()


def print_utilization(
    metrics: dict[str, list[MetricSeries]],
    names: dict[str, InstanceSnapshot] | dict[str, str],
    window: AnalysisWindow,
) -> None:
    """Print each instance's CPU and memory: average, peak hourly average and hours collected."""
    series = [item for items in metrics.values() for item in items]
    start, end = (series[0].window_start, series[0].window_end) if series else collection_window(window.duration)
    section(f"Utilization · hourly averages · {_when(start)} → {_when(end)}", indent=0)
    if not metrics:
        console.print("  [dim]No running instances to measure.[/]")
    for instance_id in sorted(metrics, key=lambda iid: (_name(names, iid).lower(), iid)):
        name = _name(names, instance_id)
        console.print(f"  [bold]{escape(name) if name else '[dim](no name)[/]'}[/] [dim]·[/] {instance_id}")
        print_series_summary(metrics[instance_id])
    console.print()


def _name(names: dict[str, InstanceSnapshot] | dict[str, str], instance_id: str) -> str:
    value = names.get(instance_id, "")
    return value.name if isinstance(value, InstanceSnapshot) else value


def get_stored_metrics(
    window_key: str = DEFAULT_WINDOW,
    region: str | None = None,
    resource: str | None = None,
) -> tuple[dict[str, str], dict[str, list[MetricSeries]]]:
    """Read stored hourly averages for the window (no CloudWatch calls)."""
    window = ANALYSIS_WINDOWS[window_key]
    account_id = aws_auth.get_identity().account_id
    region = region or aws_auth.get_session().region_name
    start, end = collection_window(window.duration)
    with db.session() as connection:
        account_ref = resources.upsert_account(connection, store.PROVIDER, account_id, datetime.now(timezone.utc))
        names, series = store.load_metrics(connection, store.scope(account_ref, region), start, end, resource)
    return names, by_instance(series)


def print_stored_metrics(
    names: dict[str, str],
    metrics: dict[str, list[MetricSeries]],
    region: str,
    window_key: str,
    hourly: bool = False,
) -> None:
    """Print the stored utilization summary; with `hourly`, every hourly average too."""
    window = ANALYSIS_WINDOWS[window_key]
    print_header("EC2 utilization", region, len(metrics), f"{window.key} window · stored hourly averages")
    if not metrics:
        warning("No stored instances match; run an analysis first: python -m app.cloud.aws.ec2 run")
        console.print()
        return
    print_utilization(metrics, names, window)
    if not hourly:
        return

    for instance_id, series in sorted(metrics.items()):
        section(f"Hourly averages · {names.get(instance_id) or '(no name)'} · {instance_id}", indent=0)
        hours = sorted({point.period_start for item in series for point in item.datapoints})
        if not hours:
            console.print("  [dim]No datapoints stored for this window.[/]")
        values = {(item.metric.key, point.period_start): point.average for item in series for point in item.datapoints}
        for hour in hours:
            readings = " [dim]·[/] ".join(
                f"{metric.label} {values[(metric.key, hour)]:.1f}%" if (metric.key, hour) in values
                else f"{metric.label} [dim]—[/]"
                for metric in METRICS
            )
            console.print(f"  [dim]{_when(hour)}[/]  {readings}")
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

    for stored in changes:
        note = "" if stored.change.significant else " · [dim]does not restart evaluation[/]"
        console.print(f"[dim]{_when(stored.detected_at)} · run #{stored.run_id}[/]{note}")
        print_change(stored.change)
        console.print()