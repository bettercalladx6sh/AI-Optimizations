"""Shared rich output helpers for every resource discovery command."""

from __future__ import annotations

from collections.abc import Iterable

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