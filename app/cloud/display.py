"""Shared rich output helpers for every resource discovery command."""

from __future__ import annotations

from collections.abc import Iterable

from datetime import datetime, timedelta, timezone

from rich.markup import escape

from app.auth.common import console

NOT_SET = "—"
LABEL_WIDTH = 18


def print_header(title: str, region: str, count: int, summary: str = "") -> None:
    """Print the command title line, e.g. 'EC2 instances · us-east-1 · 3 found'."""
    console.print()
    console.print(
        f"[bold]{escape(title)}[/] [dim]·[/] {escape(region)} [dim]·[/] {count} found"
        + (f" [dim]({escape(summary)})[/]" if summary else "")
    )
    console.print()


def section(title: str, indent: int = 2) -> None:
    """Print a section heading inside a resource block."""
    console.print(f"{' ' * indent}[bold cyan]{escape(title)}[/]")


def field(label: str, value: object, style: str = "", indent: int = 2) -> None:
    """Print one indented 'label  value' line; empty values show a dash."""
    if value in (None, ""):
        text = f"[dim]{NOT_SET}[/]"
    else:
        text = escape(str(value))
        if style:
            text = f"[{style}]{text}[/]"
    console.print(f"{' ' * indent}[dim]{label:<{LABEL_WIDTH}}[/]{text}")


def field_list(label: str, values: Iterable[object], indent: int = 2) -> None:
    """Print a label with several values, one per line, aligned under the first."""
    items = [value for value in values if value not in (None, "")]
    if not items:
        field(label, "", indent=indent)
        return
    field(label, items[0], indent=indent)
    for value in items[1:]:
        field("", value, indent=indent)

def plural(count: int, word: str, many: str | None = None) -> str:
    """Return e.g. '1 hour', '24 hours', or with an irregular form '2 queries'."""
    return f"{count} {word if count == 1 else many or word + 's'}"

def format_duration(delta: timedelta) -> str:
    """Return e.g. '9 days 8 h', '1 day', '5 h 10 min', '45 min' (rounded down to minutes)."""
    minutes = max(int(delta.total_seconds() // 60), 0)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{days} day{'s' if days != 1 else ''}"] if days else []
    if hours:
        parts.append(f"{hours} h")
    if minutes and not days:
        parts.append(f"{minutes} min")
    return " ".join(parts) or "0 min"


def format_time(moment: datetime | None) -> str:
    """Return e.g. '2026-11-10 07:00 UTC', or an empty string."""
    return f"{moment.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC" if moment else ""