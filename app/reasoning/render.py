"""Terminal output for the rightsizing recommendations: plain aligned lines, one block per instance."""

from __future__ import annotations

from typing import Any

from rich.markup import escape

from app.auth.common import console
from app.cloud.aws.ec2.windows import span_label
from app.cloud.display import field, field_list, plural

DECISIONS = {
    "INSUFFICIENT_DATA": ("INSUFFICIENT DATA", "bold yellow"),
    "KEEP_CURRENT": ("KEEP CURRENT SIZE", "bold green"),
    "DOWNSIZE_CANDIDATE": ("DOWNSIZE CANDIDATE", "bold cyan"),
    "UPSCALE_CANDIDATE": ("UPSCALE CANDIDATE", "bold magenta"),
    "INVESTIGATE": ("INVESTIGATE", "bold red"),
}
DECIDED_BY = {"evidence_gate": "evidence gate", "rules": "rules only (--no-ai)", "ai": "AI reasoning"}


def window_label(window: dict[str, Any]) -> str:
    """e.g. '30d · production · month to date · 9 days 8 h (224 hours)' from an evidence analysis_window."""
    return span_label(window["key"], window["purpose"], window["description"], window["hours"])


def print_title(label: str, region: str) -> None:
    console.print()
    console.print(f"[bold]Cloudpositive AI Engine[/] [dim]·[/] EC2 rightsizing [dim]·[/] {escape(region)} [dim]·[/] "
                  f"{escape(label)}")
    console.print()


def _money(value: float | None) -> str:
    return f"${value:,.2f}/month" if value is not None else ""


def _size(size: dict[str, Any], monthly: float | None = None) -> str:
    parts = [size["instance_type"], f"{size['vcpus'] or '?'} vCPU", f"{size['memory_mib'] or '?'} MiB"]
    if monthly is not None:
        parts.append(_money(monthly))
    return " · ".join(parts)


def _metric(metric: dict[str, Any]) -> tuple[str, str]:
    text = f"{metric['available_hours']:.1f} / {metric['expected_hours']:.1f} hours"
    if metric["p95"] is not None:
        text += f" · avg {metric['average']:.1f}% · p95 {metric['p95']:.1f}% · max {metric['maximum']:.1f}%"
        if metric["trend"]:
            text += f" · {metric['trend'].lower()}"
    return text, "green" if metric["sufficient"] else "yellow"


def print_instance(item: dict[str, Any]) -> None:
    label, style = DECISIONS[item["decision"]]
    cost = item["pricing"] or {}
    console.print(f"[bold]{escape(item['name'] or '(no name)')}[/] [dim]· {item['instance_id']} · "
                  f"{escape(item['state'])}[/]")
    field("Current", _size(item["current"], cost.get("current_monthly_usd")))
    for key, name in (("cpu_utilization", "CPU"), ("memory_utilization", "Memory")):
        text, metric_style = _metric(item["evidence"][key])
        field(name, text, metric_style)
    if item["capacity"]:
        field("Required", f"{item['capacity']['cpu_required_vcpus']:.2f} vCPU · "
                          f"{item['capacity']['memory_required_mib']:.0f} MiB (p95 x safety factor)")

    field("Decision", label, style)
    if item["recommended"]:
        field("Recommended", _size(item["recommended"], cost.get("recommended_monthly_usd")), style)
    else:
        field("Recommended", "none yet" if item["decision"] in ("INSUFFICIENT_DATA", "INVESTIGATE")
              else "no size change", "dim")
    savings = cost.get("monthly_savings_usd")
    if savings is not None:
        verb, value = ("saves", savings) if savings >= 0 else ("adds", -savings)
        field("Estimated cost", f"{verb} {_money(value)} ({abs(cost['savings_percent'] or 0):.1f}%) · "
                                f"on-demand {cost['operating_system']} list price", "green" if savings >= 0 else "yellow")
    elif cost.get("reason"):
        field("Pricing", f"not available · {cost['reason']}", "yellow")
    meta = DECIDED_BY[item["decided_by"]]
    if item["confidence"] is not None:
        meta += f" · confidence {item['confidence']:.0%}"
    field("Decided by", meta, "dim")

    field_list("Why not yet" if item["decision"] == "INSUFFICIENT_DATA" else "Why",
               [item["summary"], *(f"• {point}" for point in item["reasoning"])])
    if item["risks"]:
        field_list("Risks", [f"• {risk}" for risk in item["risks"]])
    if item["missing_evidence"]:
        field_list("Missing evidence", [f"• {entry}" for entry in item["missing_evidence"]])
    if item["eligibility"]["eligible_after"]:
        field("Eligible after", item["eligibility"]["eligible_after"], "yellow")
    field("Action", item["action"], "bold")
    console.print()


def print_report(report: dict[str, Any]) -> None:
    for item in report["recommendations"]:
        print_instance(item)
    summary = report["summary"]
    counts = " · ".join(f"[{DECISIONS[decision][1]}]{count} {DECISIONS[decision][0].lower()}[/]"
                        for decision, count in summary["by_decision"].items())
    console.print(f"[bold]{plural(summary['instance_count'], 'instance')}[/] [dim]·[/] {counts}")
    if summary["estimated_monthly_savings_usd"] is not None:
        console.print(f"[bold green]Estimated savings {_money(summary['estimated_monthly_savings_usd'])}[/] "
                      f"[dim]if every downsize candidate is applied (on-demand list prices)[/]")
    if summary["actionable"] == 0:
        console.print("[dim]No size change is recommended yet; sizes are suggested only with sufficient evidence.[/]")