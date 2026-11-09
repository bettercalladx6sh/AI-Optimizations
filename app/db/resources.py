"""Generic storage for accounts, analysis runs, resources, snapshots, changes and tags.

Provider- and service-neutral: EC2 today, other services later use the same tables.
Service-specific details (e.g. ec2_instances) are stored by each service's own store module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pymysql.connections import Connection


@dataclass(frozen=True)
class Scope:
    """Which resources a query covers: one account, region, service and resource type."""

    account_ref: int
    region: str
    service: str
    resource_type: str


@dataclass(frozen=True)
class StoredResource:
    """A resource as stored, with its current snapshot's tracked fields."""

    ref: int
    resource_id: str
    name: str
    state: str
    first_seen_at: datetime
    last_changed_at: datetime | None
    removed_at: datetime | None
    current_snapshot_id: int | None
    current_hash: str | None
    tracked: dict[str, Any] | None


@dataclass(frozen=True)
class ChangeRecord:
    """A stored change with the tracked fields before and after it."""

    change_id: int
    run_id: int
    resource_id: str
    detected_at: datetime
    change_type: str
    significant: bool
    field_changes: list[dict[str, Any]]
    before: dict[str, Any] | None
    after: dict[str, Any] | None


def to_db(moment: datetime | None) -> datetime | None:
    """MySQL DATETIME has no time zone; store naive UTC."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment else None


def from_db(moment: datetime | None) -> datetime | None:
    return moment.replace(tzinfo=timezone.utc) if moment else None


def to_json(value: Any) -> str:
    """Serialise to stable JSON (sorted keys; datetimes as ISO strings)."""
    return json.dumps(value, sort_keys=True, default=lambda obj: obj.isoformat() if isinstance(obj, datetime) else str(obj))


def _from_json(value: str | None) -> Any:
    return json.loads(value) if value else None


def upsert_account(connection: Connection, provider: str, account_id: str, seen_at: datetime) -> int:
    """Return the account's row ID, creating it on first sight."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO cloud_accounts (provider, account_id, first_seen_at, last_seen_at) "
            "VALUES (%s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(cloud_accounts.id), last_seen_at = new.last_seen_at",
            (provider, account_id, to_db(seen_at), to_db(seen_at)),
        )
        return cursor.lastrowid


def start_run(
    connection: Connection,
    scope: Scope,
    window_key: str,
    window_seconds: int,
    purpose: str,
    started_at: datetime,
) -> int:
    """Record the start of an analysis run and return its ID."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO analysis_runs (account_ref, region, service, window_key, window_seconds, purpose, started_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (scope.account_ref, scope.region, scope.service, window_key, window_seconds, purpose, to_db(started_at)),
        )
        return cursor.lastrowid


def finish_run(
    connection: Connection,
    run_id: int,
    finished_at: datetime,
    resource_count: int,
    change_count: int,
    baseline_created: bool,
) -> None:
    """Mark a run as succeeded and record its counts."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE analysis_runs SET status = 'succeeded', finished_at = %s, resource_count = %s, "
            "change_count = %s, baseline_created = %s WHERE id = %s",
            (to_db(finished_at), resource_count, change_count, baseline_created, run_id),
        )


def fail_run(connection: Connection, run_id: int, finished_at: datetime, message: str) -> None:
    """Mark a run as failed with the error message."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE analysis_runs SET status = 'failed', finished_at = %s, error_message = %s WHERE id = %s",
            (to_db(finished_at), message[:2000], run_id),
        )


def last_run_at(connection: Connection, scope: Scope) -> datetime | None:
    """Return when the previous successful run for this scope started."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT MAX(started_at) FROM analysis_runs "
            "WHERE account_ref = %s AND region = %s AND service = %s AND status = 'succeeded'",
            (scope.account_ref, scope.region, scope.service),
        )
        (started_at,) = cursor.fetchone()
    return from_db(started_at)


def load_resources(connection: Connection, scope: Scope) -> dict[str, StoredResource]:
    """Return every stored resource in the scope (including removed ones), keyed by resource ID."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT r.id, r.resource_id, r.name, r.state, r.first_seen_at, r.last_changed_at, r.removed_at, "
            "r.current_snapshot_id, s.config_hash, s.tracked "
            "FROM resources r LEFT JOIN resource_snapshots s ON s.id = r.current_snapshot_id "
            "WHERE r.account_ref = %s AND r.region = %s AND r.service = %s AND r.resource_type = %s",
            (scope.account_ref, scope.region, scope.service, scope.resource_type),
        )
        rows = cursor.fetchall()

    return {
        resource_id: StoredResource(
            ref=ref,
            resource_id=resource_id,
            name=name or "",
            state=state or "",
            first_seen_at=from_db(first_seen),
            last_changed_at=from_db(last_changed),
            removed_at=from_db(removed),
            current_snapshot_id=snapshot_id,
            current_hash=config_hash,
            tracked=_from_json(tracked),
        )
        for ref, resource_id, name, state, first_seen, last_changed, removed, snapshot_id, config_hash, tracked in rows
    }


def save_resource(
    connection: Connection,
    scope: Scope,
    resource_id: str,
    name: str,
    state: str,
    run_id: int,
    seen_at: datetime,
    changed: bool,
) -> int:
    """Insert or update a resource seen in this run and return its row ID.

    `changed` restarts the evaluation window (sets last_changed_at); a resource seen
    again after removal is marked present.
    """
    changed_at = to_db(seen_at) if changed else None
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO resources (account_ref, region, service, resource_type, resource_id, name, state, "
            "first_seen_at, first_seen_run_id, last_seen_at, last_seen_run_id, last_changed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(resources.id), name = new.name, state = new.state, "
            "last_seen_at = new.last_seen_at, last_seen_run_id = new.last_seen_run_id, "
            "last_changed_at = COALESCE(new.last_changed_at, resources.last_changed_at), removed_at = NULL",
            (
                scope.account_ref, scope.region, scope.service, scope.resource_type, resource_id,
                name or None, state or None, to_db(seen_at), run_id, to_db(seen_at), run_id, changed_at,
            ),
        )
        return cursor.lastrowid


def save_snapshot(
    connection: Connection,
    resource: int,
    run_id: int,
    captured_at: datetime,
    tracked: Mapping[str, Any],
    configuration: Mapping[str, Any],
    current: StoredResource | None,
) -> int:
    """Store the resource's configuration and make it current; return the snapshot ID.

    A new snapshot row is written only when the configuration differs from the current
    one, so the table holds every distinct configuration the resource has had.
    """
    document = to_json(configuration)
    config_hash = hashlib.sha256(document.encode("utf-8")).hexdigest()
    if current and current.current_snapshot_id and current.current_hash == config_hash:
        return current.current_snapshot_id

    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO resource_snapshots (resource_ref, run_id, captured_at, config_hash, tracked, configuration) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (resource, run_id, to_db(captured_at), config_hash, to_json(tracked), document),
        )
        snapshot_id = cursor.lastrowid
        cursor.execute("UPDATE resources SET current_snapshot_id = %s WHERE id = %s", (snapshot_id, resource))
    return snapshot_id


def mark_removed(connection: Connection, resource: int, removed_at: datetime) -> None:
    """Flag a resource that no longer exists in the cloud account."""
    with connection.cursor() as cursor:
        cursor.execute("UPDATE resources SET removed_at = %s WHERE id = %s", (to_db(removed_at), resource))


def record_change(
    connection: Connection,
    resource: int,
    run_id: int,
    detected_at: datetime,
    change_type: str,
    significant: bool,
    field_changes: Iterable[Mapping[str, Any]],
    before_snapshot_id: int | None,
    after_snapshot_id: int | None,
) -> None:
    """Store one detected change, linked to the snapshots before and after it."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO resource_changes (resource_ref, run_id, change_type, significant, detected_at, "
            "field_changes, before_snapshot_id, after_snapshot_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (resource, run_id, change_type, significant, to_db(detected_at),
             to_json(list(field_changes)), before_snapshot_id, after_snapshot_id),
        )


def replace_tags(connection: Connection, resource: int, tags: Iterable[tuple[str, str]]) -> None:
    """Replace the resource's stored tags with its current tags."""
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM resource_tags WHERE resource_ref = %s", (resource,))
        rows = [(resource, key, value) for key, value in tags]
        if rows:
            cursor.executemany(
                "INSERT INTO resource_tags (resource_ref, tag_key, tag_value) VALUES (%s, %s, %s)", rows
            )


def list_changes(
    connection: Connection,
    scope: Scope,
    resource: str | None = None,
    limit: int = 20,
) -> list[ChangeRecord]:
    """Return stored changes in the scope, newest first, optionally for one resource ID or name."""
    query = (
        "SELECT c.id, c.run_id, r.resource_id, c.detected_at, c.change_type, c.significant, "
        "c.field_changes, b.tracked, a.tracked "
        "FROM resource_changes c "
        "JOIN resources r ON r.id = c.resource_ref "
        "LEFT JOIN resource_snapshots b ON b.id = c.before_snapshot_id "
        "LEFT JOIN resource_snapshots a ON a.id = c.after_snapshot_id "
        "WHERE r.account_ref = %s AND r.region = %s AND r.service = %s AND r.resource_type = %s"
    )
    params: list[Any] = [scope.account_ref, scope.region, scope.service, scope.resource_type]
    if resource:
        query += " AND (r.resource_id = %s OR r.name = %s)"
        params += [resource, resource]
    query += " ORDER BY c.detected_at DESC, c.id DESC LIMIT %s"
    params.append(limit)

    with connection.cursor() as cursor:
        cursor.execute(query, params)
        rows = cursor.fetchall()

    return [
        ChangeRecord(
            change_id=change_id,
            run_id=run_id,
            resource_id=resource_id,
            detected_at=from_db(detected_at),
            change_type=change_type,
            significant=bool(significant),
            field_changes=_from_json(fields) or [],
            before=_from_json(before),
            after=_from_json(after),
        )
        for change_id, run_id, resource_id, detected_at, change_type, significant, fields, before, after in rows
    ]