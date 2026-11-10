"""The clouds the AI engine evaluates: everything provider-specific in one adapter per cloud.

The engine itself (gate, sizing, pricing, AI, validation) is the same for every provider and
reads evidence documents of the same shape. An adapter supplies what differs:
    the evidence pipeline and its validator, the default scope (AWS region / Azure location)
    the candidate sizes of an instance's family and how it is priced
    the configuration details the AI sees and the wording of monitor and memory hints
    the recommendations schema and pricing source

    aws    EC2 instances      CloudWatch       AWS Price List API (on-demand)
    azure  Azure VMs          Azure Monitor    Azure Retail Prices API (pay-as-you-go)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.auth import aws_auth
from app.cloud.aws.ec2 import evidence as ec2_evidence
from app.cloud.aws.ec2 import pipeline as ec2_pipeline
from app.cloud.aws.ec2 import pricing as ec2_pricing
from app.cloud.azure.vm import evidence as azure_evidence
from app.cloud.azure.vm import pipeline as azure_pipeline
from app.cloud.azure.vm import pricing as azure_pricing
from app.cloud.azure.vm import skus
from app.cloud.display import StepPrinter
from app.cloud.pricing import InstancePrice, PricingBasis
from app.cloud.schema import ValidationIssue
from app.reasoning import sizing
from app.reasoning.config import ReasoningError

SCHEMAS = Path(__file__).resolve().parent / "schemas"

Configuration = dict[str, Any]


@dataclass(frozen=True)
class Provider:
    """What the engine needs to know about one cloud."""

    key: str                         # evidence source.provider
    title: str                       # e.g. "EC2", shown in titles
    cloud: str                       # e.g. "AWS", used in messages and the AI prompt
    monitor: str                     # where the metrics come from
    scope_name: str                  # "region" or "location"
    price_kind: str                  # "on-demand" or "pay-as-you-go"
    pricing_source: str
    candidate_scope: str
    ai_format_name: str              # name of the structured-output format sent to OpenAI
    schema_path: Path
    pipeline_steps: tuple[str, ...]
    run_pipeline: Callable[[str, str | None, StepPrinter], dict[str, Any]]
    validate_evidence: Callable[[dict[str, Any]], list[ValidationIssue]]
    default_scope: Callable[[], str | None]
    candidate_sizes: Callable[[Configuration, sizing.InstanceSize, str], tuple[sizing.InstanceSize, ...]]
    pricing_basis: Callable[[Configuration], PricingBasis]
    fetch_prices: Callable[[Configuration, str, tuple[str, ...], PricingBasis], dict[str, InstancePrice]]
    instance_details: Callable[[Configuration], dict[str, Any]]
    memory_evidence: Callable[[dict[str, Any]], str]

    @property
    def ai_prices(self) -> str:
        return f"{self.cloud} {self.price_kind} list prices"


# ---------------------------------------------------------------- AWS EC2


def _ec2_sizes(configuration: Configuration, current: sizing.InstanceSize, region: str) -> tuple[sizing.InstanceSize, ...]:
    return sizing.family_sizes(region, current.instance_type.split(".", 1)[0])


def _ec2_prices(configuration: Configuration, region: str, types: tuple[str, ...],
                basis: PricingBasis) -> dict[str, InstancePrice]:
    return ec2_pricing.fetch_prices(region, types, basis.operating_system, basis.tenancy)


def _ec2_details(configuration: Configuration) -> dict[str, Any]:
    return {key: configuration[key] for key in ("current_generation", "platform", "lifecycle")}


AWS = Provider(
    key="aws",
    title="EC2",
    cloud="AWS",
    monitor="CloudWatch",
    scope_name="region",
    price_kind="on-demand",
    pricing_source="AWS Price List API (on-demand)",
    candidate_scope="same instance family",
    ai_format_name="ec2_rightsizing_reasoning",
    schema_path=SCHEMAS / "ec2_recommendations.schema.json",
    pipeline_steps=ec2_pipeline.STEPS,
    run_pipeline=lambda window, region, steps: ec2_pipeline.run_pipeline(window, region, steps=steps).evidence,
    validate_evidence=ec2_evidence.validate_evidence,
    default_scope=lambda: aws_auth.get_session().region_name,
    candidate_sizes=_ec2_sizes,
    pricing_basis=lambda configuration: ec2_pricing.pricing_basis(configuration),
    fetch_prices=_ec2_prices,
    instance_details=_ec2_details,
    memory_evidence=lambda evidence: "Memory utilization (CloudWatch agent publishing EC2/Custom mem_used_percent)",
)


# ---------------------------------------------------------------- Azure VMs


def _azure_sizes(configuration: Configuration, current: sizing.InstanceSize,
                 region: str) -> tuple[sizing.InstanceSize, ...]:
    """Every size of the VM's family it can be resized to in its location (plus the current size)."""
    members = [sizing.InstanceSize(size.name, size.vcpus, size.memory_mib)
               for size in skus.family_members(configuration["location"], current.instance_type)]
    if members and all(size.instance_type.lower() != current.instance_type.lower() for size in members):
        members.append(current)
    return tuple(sorted(members, key=lambda size: size.order))


def _azure_prices(configuration: Configuration, region: str, types: tuple[str, ...],
                  basis: PricingBasis) -> dict[str, InstancePrice]:
    return azure_pricing.fetch_prices(configuration["location"], types, azure_pricing.meter_os(basis))


def _azure_details(configuration: Configuration) -> dict[str, Any]:
    return {key: configuration[key] for key in ("os_type", "license_type", "priority")}


def _azure_memory(evidence: dict[str, Any]) -> str:
    if evidence["collection"]["memory_source"] == "log_analytics":
        return ("Memory utilization (VM insights: Azure Monitor Agent sending InsightsMetrics Memory/AvailableMB "
                "to the Log Analytics workspace)")
    return ("Memory utilization (Azure Monitor Available Memory Bytes, or VM insights with "
            "AZURE_LOG_ANALYTICS_WORKSPACE_ID set)")


AZURE = Provider(
    key="azure",
    title="Azure VM",
    cloud="Azure",
    monitor="Azure Monitor",
    scope_name="location",
    price_kind="pay-as-you-go",
    pricing_source=azure_pricing.SOURCE,
    candidate_scope="same VM size family",
    ai_format_name="azure_vm_rightsizing_reasoning",
    schema_path=SCHEMAS / "azure_vm_recommendations.schema.json",
    pipeline_steps=azure_pipeline.STEPS,
    run_pipeline=lambda window, location, steps: azure_pipeline.run_pipeline(window, location, steps=steps).evidence,
    validate_evidence=azure_evidence.validate_evidence,
    default_scope=lambda: None,
    candidate_sizes=_azure_sizes,
    pricing_basis=lambda configuration: azure_pricing.pricing_basis(configuration),
    fetch_prices=_azure_prices,
    instance_details=_azure_details,
    memory_evidence=_azure_memory,
)

PROVIDERS = {provider.key: provider for provider in (AWS, AZURE)}


def provider_for(document: dict[str, Any]) -> Provider:
    """The provider of an evidence or recommendations document (by source.provider)."""
    key = (document.get("source") or {}).get("provider") if isinstance(document, dict) else None
    if key not in PROVIDERS:
        raise ReasoningError(f"unknown evidence provider '{key}'",
                             (f"Evidence must come from one of: {', '.join(PROVIDERS)}",))
    return PROVIDERS[key]
