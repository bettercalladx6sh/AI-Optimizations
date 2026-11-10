"""Cloudpositive AI Engine: from validated evidence to priced rightsizing recommendations.

Steps (after the evidence pipeline, or after loading an evidence file):
    1. Check evidence sufficiency      evidence gate, no AI (eligibility.py)
    2. Validate candidate sizes        capacity and family sizes (sizing.py)
    3. Fetch on-demand pricing         real-time AWS Price List prices (ec2/pricing.py)
    4. AI reasoning                    eligible instances only (ai.py)
    5. Validate recommendations        JSON Schema + policy checks
    6. Update recommendations file     one file per account, region and window, replaced

Only DOWNSIZE_CANDIDATE and UPSCALE_CANDIDATE carry a recommended size, and only a size the
rules validated. Costs and savings come only from prices AWS returned in this run; when a
price is missing, no cost is shown. Instances without sufficient evidence get
INSUFFICIENT_DATA and are told what to collect and when to run again.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from rich.markup import escape

from app.auth.common import console
from app.config import CPU_SAFETY_FACTOR, MEMORY_SAFETY_FACTOR, MIN_COVERAGE_PERCENT, MIN_EVIDENCE_HOURS
from app.cloud.aws.ec2 import pipeline, pricing
from app.cloud.aws.ec2.evidence import timestamp, validate_evidence
from app.cloud.display import plural
from app.cloud.files import PROJECT_ROOT, display_path, write_json
from app.cloud.schema import EvidenceValidationError, ValidationIssue, validate_document
from app.reasoning import eligibility, sizing
from app.reasoning.ai import AiReasoner
from app.reasoning.config import ReasoningError, ReasoningSettings

SCHEMA_VERSION = "1.0"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "ec2_recommendations.schema.json"
RECOMMENDATIONS_DIR = PROJECT_ROOT / "recommendations"
ACTIONABLE = ("DOWNSIZE_CANDIDATE", "UPSCALE_CANDIDATE")
PRICING_SOURCE = "AWS Price List API (on-demand)"
REASONING_STEPS = (
    "Check evidence sufficiency",
    "Validate candidate sizes",
    "Fetch on-demand pricing",
    "AI reasoning",
    "Validate recommendations",
    "Update recommendations file",
)

StepCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class EngineResult:
    report: dict[str, Any]
    path: Path | None


@dataclass(frozen=True)
class Priced:
    """Prices found for one instance's current size and candidates."""

    basis: pricing.PricingBasis
    prices: dict[str, pricing.InstancePrice]
    fetched_at: str | None

    def hourly(self) -> dict[str, float]:
        return {instance_type: price.hourly_usd for instance_type, price in self.prices.items()}


def load_evidence_file(path: Path) -> dict[str, Any]:
    """Read an evidence JSON file and refuse it unless it passes evidence validation."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReasoningError(f"evidence file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ReasoningError(f"evidence file is not valid JSON: {exc}") from None
    issues = validate_evidence(document)
    if issues:
        raise EvidenceValidationError(
            f"evidence file failed validation with {plural(len(issues), 'issue')}",
            [str(issue) for issue in issues[:10]],
        )
    return document


def default_path(evidence: dict[str, Any]) -> Path:
    """recommendations/aws/ec2/<account>/<region>/<window>.json under the project root."""
    source = evidence["source"]
    window = evidence["analysis_window"]["key"]
    return RECOMMENDATIONS_DIR / "aws" / "ec2" / source["account_id"] / source["region"] / f"{window}.json"


def _current_size(instance: dict[str, Any]) -> sizing.InstanceSize:
    configuration = instance["configuration"]
    memory = configuration["memory_gib"]
    return sizing.InstanceSize(
        configuration["instance_type"],
        configuration["vcpus"] or 0,
        int(round(memory * 1024)) if memory else 0,
    )


def _size(size: sizing.InstanceSize | None) -> dict[str, Any] | None:
    if size is None:
        return None
    return {"instance_type": size.instance_type, "vcpus": size.vcpus or None, "memory_mib": size.memory_mib or None}


def _metric_json(metric: eligibility.MetricEvidence) -> dict[str, Any]:
    return {
        "expected_hours": metric.expected_hours,
        "available_hours": metric.available_hours,
        "coverage_percent": metric.coverage_percent,
        "datapoints": metric.datapoints,
        "average": metric.average,
        "p95": metric.p95,
        "maximum": metric.maximum,
        "trend": metric.trend,
        "volatility": metric.volatility,
        "sufficient": metric.sufficient,
    }


def _candidates_json(result: sizing.SizingResult, priced: Priced) -> list[dict[str, Any]]:
    hourly = priced.hourly()
    return [
        {**_size(candidate.size), "safe": candidate.safe, "reasons": list(candidate.reasons),
         "hourly_usd": hourly.get(candidate.size.instance_type)}
        for candidate in result.candidates
    ]


def _pricing_json(priced: Priced, current: sizing.InstanceSize,
                  recommended: sizing.InstanceSize | None) -> dict[str, Any]:
    """Cost of the current and recommended size; every figure comes from a fetched price."""
    current_price = priced.prices.get(current.instance_type)
    target = priced.prices.get(recommended.instance_type) if recommended else None
    reason = priced.basis.reason
    if reason is None and current_price is None:
        reason = f"AWS returned no on-demand price for {current.instance_type}"
    elif reason is None and recommended and target is None:
        reason = f"AWS returned no on-demand price for {recommended.instance_type}"

    savings = percent = None
    if current_price and target:
        hourly_difference = current_price.hourly_usd - target.hourly_usd
        savings = round(hourly_difference * pricing.HOURS_PER_MONTH, 2)
        percent = round(hourly_difference / current_price.hourly_usd * 100, 1)
    return {
        "available": current_price is not None,
        "reason": reason,
        "source": PRICING_SOURCE,
        "operating_system": priced.basis.operating_system,
        "tenancy": priced.basis.tenancy,
        "currency": "USD",
        "hours_per_month": pricing.HOURS_PER_MONTH,
        "current_hourly_usd": current_price.hourly_usd if current_price else None,
        "current_monthly_usd": current_price.monthly_usd if current_price else None,
        "recommended_hourly_usd": target.hourly_usd if target else None,
        "recommended_monthly_usd": target.monthly_usd if target else None,
        "monthly_savings_usd": savings,
        "savings_percent": percent,
        "effective_date": current_price.effective_date if current_price else None,
        "fetched_at": priced.fetched_at,
    }


def _ai_payload(evidence: dict[str, Any], instance: dict[str, Any], current: sizing.InstanceSize,
                metrics: dict[str, eligibility.MetricEvidence], result: sizing.SizingResult,
                priced: Priced) -> dict[str, Any]:
    """Everything the AI needs for one instance, and nothing it does not (no names, tags, account)."""
    configuration, window = instance["configuration"], evidence["analysis_window"]
    return {
        "analysis_window": {key: window[key] for key in ("key", "purpose", "description", "hours")},
        "instance": {
            "state": instance["state"],
            "instance_type": current.instance_type,
            "family": configuration["family"],
            "vcpus": current.vcpus,
            "memory_mib": current.memory_mib,
            "architecture": configuration["architecture"],
            "burstable": configuration["burstable"],
            "current_generation": configuration["current_generation"],
            "platform": configuration["platform"],
            "lifecycle": configuration["lifecycle"],
        },
        "evidence": {key: _metric_json(metric) for key, metric in metrics.items()},
        "data_quality_issues": [issue["code"] for issue in instance["data_quality"]["issues"]],
        "capacity": {**asdict(result.capacity), "cpu_safety_factor": CPU_SAFETY_FACTOR,
                     "memory_safety_factor": MEMORY_SAFETY_FACTOR},
        "candidates": _candidates_json(result, priced),
        "pricing": _pricing_json(priced, current, result.recommended),
        "rule_based_decision": {
            "decision": result.decision,
            "recommended_instance_type": result.recommended.instance_type if result.recommended else None,
        },
        "candidate_scope": "same instance family",
    }


def _rules_text(decision: str, current: sizing.InstanceSize, result: sizing.SizingResult,
                cost: dict[str, Any]) -> tuple[str, list[str], str]:
    """Summary, reasoning and action for a rule-based decision (used with --no-ai)."""
    capacity = result.capacity
    points = [
        f"CPU p95 {capacity.cpu_p95:.1f}% x {CPU_SAFETY_FACTOR:g} safety needs {capacity.cpu_required_vcpus:.2f} "
        f"of {current.vcpus} vCPU.",
        f"Memory p95 {capacity.memory_p95:.1f}% x {MEMORY_SAFETY_FACTOR:g} safety needs "
        f"{capacity.memory_required_mib:.0f} of {current.memory_mib} MiB.",
    ]
    if cost["current_monthly_usd"] is not None:
        points.append(f"{current.instance_type} costs ${cost['current_monthly_usd']:,.2f}/month on demand.")
    if decision in ACTIONABLE:
        target = result.recommended
        summary = (f"{target.instance_type} is the lowest-cost size in the family that covers the required "
                   f"capacity ({target.vcpus} vCPU, {target.memory_mib} MiB).")
        if cost["recommended_monthly_usd"] is not None:
            points.append(f"{target.instance_type} costs ${cost['recommended_monthly_usd']:,.2f}/month on demand.")
        action = f"Review and test {target.instance_type} before changing {current.instance_type}."
    elif decision == "KEEP_CURRENT":
        summary = f"{current.instance_type} is already the lowest-cost size in its family that covers the required capacity."
        action = "No change needed; keep monitoring."
    else:
        summary = "No size in the instance family covers the required capacity."
        action = "Investigate the workload before choosing a different instance family."
    return summary, points, action


def _fetch_pricing(region: str, instances: list[dict[str, Any]],
                   sizings: dict[str, sizing.SizingResult]) -> dict[str, Priced]:
    """One Price List request per family/OS/tenancy (cached), for each eligible instance."""
    priced = {}
    for instance in instances:
        instance_id = instance["instance_id"]
        basis = pricing.pricing_basis(instance["configuration"])
        if basis.reason:
            priced[instance_id] = Priced(basis, {}, None)
            continue
        types = tuple(sorted({candidate.size.instance_type for candidate in sizings[instance_id].candidates}
                             | {instance["configuration"]["instance_type"]}))
        prices = pricing.fetch_prices(region, types, basis.operating_system, basis.tenancy)
        priced[instance_id] = Priced(basis, prices, pricing.fetched_at())
    return priced


def decide(evidence: dict[str, Any], reasoner: AiReasoner | None,
           on_step: StepCallback) -> list[dict[str, Any]]:
    """Gate, size, price and (optionally) reason about every instance of the evidence."""
    window, region = evidence["analysis_window"], evidence["source"]["region"]
    instances = evidence["instances"]
    gates = {instance["instance_id"]: eligibility.check(instance, window) for instance in instances}
    eligible = [instance for instance in instances if gates[instance["instance_id"]].eligible]
    on_step(REASONING_STEPS[0], f"{len(eligible)} of {plural(len(instances), 'instance')} have sufficient evidence")

    sizings: dict[str, sizing.SizingResult] = {}
    for instance in eligible:
        metrics = eligibility.metrics_of(instance)
        sizings[instance["instance_id"]] = sizing.size_instance(
            region, _current_size(instance),
            metrics["cpu_utilization"].p95, metrics["memory_utilization"].p95,
        )
    checked = sum(len(result.candidates) for result in sizings.values())
    on_step(REASONING_STEPS[1], f"{plural(checked, 'candidate size')} checked for {plural(len(sizings), 'instance')}"
            if sizings else "skipped · no instance has sufficient evidence")

    priced = _fetch_pricing(region, eligible, sizings)
    for instance in eligible:
        instance_id = instance["instance_id"]
        sizings[instance_id] = sizing.apply_prices(sizings[instance_id], _current_size(instance),
                                                   priced[instance_id].hourly())
    price_count = len({key for item in priced.values() for key in item.prices})
    on_step(REASONING_STEPS[2], f"{plural(price_count, 'instance type')} priced · {region} · {PRICING_SOURCE}"
            if eligible else "skipped · nothing to price yet")

    records = []
    for instance in instances:
        instance_id, gate = instance["instance_id"], gates[instance["instance_id"]]
        current, metrics = _current_size(instance), eligibility.metrics_of(instance)
        record = {
            "instance_id": instance_id,
            "name": instance["name"],
            "state": instance["state"],
            "current": _size(current),
            "evidence": {key: _metric_json(metric) for key, metric in metrics.items()},
            "eligibility": {"eligible": gate.eligible, "reasons": list(gate.reasons), "eligible_after": gate.eligible_after},
            "capacity": None,
            "candidates": [],
            "pricing": None,
        }
        if not gate.eligible:
            record.update(
                decision="INSUFFICIENT_DATA", recommended=None, confidence=None, decided_by="evidence_gate",
                summary="Not enough verified evidence to evaluate this instance; no size is recommended yet.",
                reasoning=list(gate.reasons), risks=[], missing_evidence=list(gate.missing_evidence),
                action=gate.action,
            )
            records.append(record)
            continue

        result, cost_basis = sizings[instance_id], priced[instance_id]
        record["capacity"] = asdict(result.capacity)
        record["candidates"] = _candidates_json(result, cost_basis)
        if reasoner is None:
            summary, points, action = _rules_text(result.decision, current, result,
                                                  _pricing_json(cost_basis, current, result.recommended))
            record.update(decision=result.decision, confidence=None, decided_by="rules", summary=summary,
                          reasoning=points, risks=[], missing_evidence=[], action=action)
        else:
            with console.status(f"[cyan]AI reasoning[/] {escape(instance['name'] or instance_id)} …", spinner="dots"):
                answer = reasoner.reason(_ai_payload(evidence, instance, current, metrics, result, cost_basis))
            record.update(decision=answer.decision, confidence=answer.confidence, decided_by="ai",
                          summary=answer.summary, reasoning=list(answer.reasoning), risks=list(answer.risks),
                          missing_evidence=list(answer.missing_evidence), action=answer.action)
        recommended = result.recommended if record["decision"] in ACTIONABLE else None
        record["recommended"] = _size(recommended)
        record["pricing"] = _pricing_json(cost_basis, current, recommended)
        records.append(record)

    if reasoner is None:
        on_step(REASONING_STEPS[3], "skipped (--no-ai) · rule-based decisions only")
    else:
        on_step(REASONING_STEPS[3], f"{plural(len(eligible), 'instance')} reasoned by {reasoner.model}"
                if eligible else "skipped · nothing to evaluate yet")
    return records


def _policy_issues(report: dict[str, Any]) -> list[ValidationIssue]:
    """Rules the JSON Schema cannot express."""
    issues = []
    for index, item in enumerate(report["recommendations"]):
        path = f"$.recommendations[{index}]"
        if item["decision"] in ACTIONABLE:
            safe = {candidate["instance_type"] for candidate in item["candidates"] if candidate["safe"]}
            if not item["recommended"] or item["recommended"]["instance_type"] not in safe:
                issues.append(ValidationIssue(f"{path}.recommended", "must be one of the validated safe candidates"))
        elif item["recommended"] is not None:
            issues.append(ValidationIssue(f"{path}.recommended", f"must be null for {item['decision']}"))
        if item["decision"] == "INSUFFICIENT_DATA" and item["eligibility"]["eligible"]:
            issues.append(ValidationIssue(f"{path}.decision", "INSUFFICIENT_DATA requires eligibility.eligible = false"))
        if item["decision"] != "INSUFFICIENT_DATA" and not item["eligibility"]["eligible"]:
            issues.append(ValidationIssue(f"{path}.decision", "an ineligible instance must be INSUFFICIENT_DATA"))
        cost = item["pricing"]
        if item["decision"] == "DOWNSIZE_CANDIDATE" and cost and cost["monthly_savings_usd"] is not None \
                and cost["monthly_savings_usd"] <= 0:
            issues.append(ValidationIssue(f"{path}.pricing", "a downsize must lower the on-demand cost"))
    if report["summary"]["instance_count"] != len(report["recommendations"]):
        issues.append(ValidationIssue("$.summary.instance_count", "does not match the recommendations"))
    return issues


def build_report(evidence: dict[str, Any], records: list[dict[str, Any]],
                 settings: ReasoningSettings | None) -> dict[str, Any]:
    window = evidence["analysis_window"]
    decisions = Counter(record["decision"] for record in records)
    savings = [record["pricing"]["monthly_savings_usd"] for record in records
               if record["decision"] == "DOWNSIZE_CANDIDATE" and record["pricing"]
               and record["pricing"]["monthly_savings_usd"] is not None]
    return {
        "schema_version": SCHEMA_VERSION,
        "report_id": str(uuid.uuid4()),
        "generated_at": timestamp(datetime.now(timezone.utc)),
        "engine": {
            "name": "Cloudpositive AI Engine",
            "ai_used": settings is not None,
            "ai_provider": "openai" if settings else None,
            "ai_model": settings.model if settings else None,
        },
        "source": {**{key: evidence["source"][key] for key in ("provider", "service", "account_id", "region",
                                                                "analysis_run_id")},
                   "evidence_id": evidence["evidence_id"]},
        "analysis_window": {key: window[key] for key in ("key", "purpose", "description", "start", "end", "hours")},
        "policy": {
            "min_coverage_percent": MIN_COVERAGE_PERCENT,
            "min_evidence_hours": MIN_EVIDENCE_HOURS,
            "cpu_safety_factor": CPU_SAFETY_FACTOR,
            "memory_safety_factor": MEMORY_SAFETY_FACTOR,
            "candidate_scope": "same instance family",
            "pricing_evaluated": any(record["pricing"] and record["pricing"]["available"] for record in records),
            "pricing_source": PRICING_SOURCE,
        },
        "summary": {
            "instance_count": len(records),
            "by_decision": dict(sorted(decisions.items())),
            "actionable": sum(decisions[decision] for decision in ACTIONABLE),
            "estimated_monthly_savings_usd": round(sum(savings), 2) if savings else None,
        },
        "recommendations": records,
    }


def validate_report(report: dict[str, Any]) -> list[ValidationIssue]:
    issues = validate_document(report, SCHEMA_PATH)
    return issues or _policy_issues(report)


def run(
    window_key: str,
    region: str | None,
    evidence: dict[str, Any] | None,
    settings: ReasoningSettings | None,
    output: Path | None,
    on_step: StepCallback,
    reasoner: AiReasoner | None = None,
) -> EngineResult:
    """Prepare evidence (unless given), decide every instance, validate and write the recommendations."""
    if evidence is None:
        evidence = pipeline.run_pipeline(window_key, region, steps=on_step).evidence

    if settings and reasoner is None:
        reasoner = AiReasoner(settings)
    records = decide(evidence, reasoner if settings else None, on_step)

    report = build_report(evidence, records, settings)
    issues = validate_report(report)
    if issues:
        raise EvidenceValidationError(
            f"recommendations failed validation with {plural(len(issues), 'issue')}; no file was written",
            [str(issue) for issue in issues[:10]],
        )
    on_step(REASONING_STEPS[4], f"schema {SCHEMA_VERSION} · policy checks passed")

    path = output or default_path(evidence)
    updated = write_json(report, path)
    on_step(REASONING_STEPS[5], f"{'updated' if updated else 'created'} {display_path(path)}")
    return EngineResult(report, path)