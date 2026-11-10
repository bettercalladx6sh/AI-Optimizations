"""Real-time EC2 on-demand prices from the AWS Price List Query API (pricing:GetProducts).

One request per instance family (all sizes at once with an ANY_OF filter) returns the
current public on-demand list price per hour for the region, operating system and tenancy
of the instance. Results are cached for the run, and every request goes through the shared
backoff so throttling slows the run down instead of failing it.

Only prices AWS returns are used: an instance type with no single, unambiguous on-demand
price gets no price, and nothing is estimated. Reserved, Spot and Savings Plans prices are
not considered.

Usage:
    python -m app.cloud.aws.ec2.pricing t3a.medium t3a.small     # prices in the .env region
    python -m app.cloud.aws.ec2.pricing m5.large --os Windows --region eu-west-1
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from typing import Any

from app.auth import aws_auth
from app.auth.common import AuthError, console, hint, status
from app.cloud.aws.backoff import with_backoff
from app.cloud.aws.errors import aws_errors
from app.cloud.pricing import HOURS_PER_MONTH, InstancePrice, PricingBasis, fetched_at

__all__ = ["HOURS_PER_MONTH", "InstancePrice", "PricingBasis", "fetch_prices", "fetched_at", "pricing_basis"]

PRICING_API_REGION = "us-east-1"     # the Price List API endpoint; it serves prices for every region
MAX_RESULTS = 100

# EC2 PlatformDetails -> Price List operatingSystem
OPERATING_SYSTEMS = {
    "Linux/UNIX": "Linux",
    "Windows": "Windows",
    "Red Hat Enterprise Linux": "RHEL",
    "SUSE Linux": "SUSE",
    "Ubuntu Pro": "Ubuntu Pro",
}
# EC2 Placement.Tenancy -> Price List tenancy
TENANCIES = {"default": "Shared", "dedicated": "Dedicated", "host": "Host"}


def pricing_basis(configuration: dict[str, Any]) -> PricingBasis:
    """Map an instance configuration to Price List filters; explain when it cannot be priced."""
    if configuration.get("lifecycle") not in (None, "on-demand"):
        return PricingBasis(None, None, f"{configuration['lifecycle']} instance; on-demand list prices do not apply")
    platform = configuration.get("platform")
    operating_system = OPERATING_SYSTEMS.get(platform)
    if operating_system is None:
        return PricingBasis(None, None, f"platform '{platform}' is not mapped to a Price List operating system")
    tenancy = TENANCIES.get(configuration.get("tenancy") or "default")
    if tenancy is None:
        return PricingBasis(None, None, f"tenancy '{configuration.get('tenancy')}' is not supported")
    return PricingBasis(operating_system, tenancy)


def _filters(instance_types: tuple[str, ...], region: str, operating_system: str, tenancy: str) -> list[dict[str, str]]:
    def term(field: str, value: str, kind: str = "TERM_MATCH") -> dict[str, str]:
        return {"Type": kind, "Field": field, "Value": value}

    return [
        term("instanceType", ",".join(instance_types), "ANY_OF"),
        term("regionCode", region),
        term("operatingSystem", operating_system),
        term("tenancy", tenancy),
        term("preInstalledSw", "NA"),
        term("capacitystatus", "Used"),
        term("licenseModel", "No License required"),
        term("marketoption", "OnDemand"),
    ]


def _on_demand_hourly(product: dict[str, Any]) -> tuple[float, str | None] | None:
    """The single hourly USD price of a product's on-demand terms, or None when not exactly one."""
    prices = []
    for term in product.get("terms", {}).get("OnDemand", {}).values():
        for dimension in term.get("priceDimensions", {}).values():
            usd = dimension.get("pricePerUnit", {}).get("USD")
            if dimension.get("unit") == "Hrs" and usd is not None:
                prices.append((float(usd), term.get("effectiveDate")))
    return prices[0] if len(prices) == 1 else None


@lru_cache(maxsize=128)
def fetch_prices(
    region: str,
    instance_types: tuple[str, ...],
    operating_system: str = "Linux",
    tenancy: str = "Shared",
) -> dict[str, InstancePrice]:
    """On-demand hourly prices for the given instance types, keyed by type (missing = not priced)."""
    if not instance_types:
        return {}
    client = aws_auth.get_client("pricing", region=PRICING_API_REGION)
    request = {
        "ServiceCode": "AmazonEC2",
        "FormatVersion": "aws_v1",
        "MaxResults": MAX_RESULTS,
        "Filters": _filters(tuple(sorted(set(instance_types))), region, operating_system, tenancy),
    }
    found: dict[str, list[InstancePrice]] = {}
    with aws_errors("pricing:GetProducts"):
        while True:
            response = with_backoff(client.get_products, label="pricing:GetProducts", **request)
            for item in response.get("PriceList", []):
                product = json.loads(item) if isinstance(item, str) else item
                attributes = product.get("product", {}).get("attributes", {})
                hourly = _on_demand_hourly(product)
                if "BoxUsage" not in attributes.get("usagetype", "") or hourly is None or hourly[0] <= 0:
                    continue
                instance_type = attributes["instanceType"]
                found.setdefault(instance_type, []).append(InstancePrice(
                    instance_type=instance_type,
                    region=region,
                    operating_system=operating_system,
                    tenancy=tenancy,
                    hourly_usd=hourly[0],
                    effective_date=hourly[1],
                    published_at=product.get("publicationDate"),
                ))
            if not response.get("NextToken"):
                break
            request["NextToken"] = response["NextToken"]

    # Keep a type only when AWS returned one unambiguous price for it.
    return {
        instance_type: prices[0]
        for instance_type, prices in found.items()
        if len({price.hourly_usd for price in prices}) == 1
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cloud.aws.ec2.pricing",
        description="Show current EC2 on-demand list prices from the AWS Price List API.",
    )
    parser.add_argument("instance_types", nargs="+", metavar="TYPE", help="instance types, e.g. t3a.medium")
    parser.add_argument("--region", help="region to price (default: AWS_DEFAULT_REGION from .env)")
    parser.add_argument("--os", default="Linux", choices=sorted(set(OPERATING_SYSTEMS.values())),
                        help="operating system (default: Linux)")
    parser.add_argument("--tenancy", default="Shared", choices=sorted(set(TENANCIES.values())),
                        help="tenancy (default: Shared)")
    args = parser.parse_args(argv)

    try:
        region = args.region or aws_auth.get_session().region_name
        prices = fetch_prices(region, tuple(args.instance_types), args.os, args.tenancy)
    except AuthError as exc:
        status(False, f"Pricing failed: {exc}")
        hint(exc.hints)
        return 1

    console.print()
    console.print(f"[bold]EC2 on-demand prices[/] [dim]·[/] {region} · {args.os} · {args.tenancy} tenancy")
    console.print()
    for instance_type in args.instance_types:
        price = prices.get(instance_type)
        if price is None:
            console.print(f"  [yellow]{instance_type:<16}[/] [dim]no on-demand price returned[/]")
            continue
        console.print(f"  [bold]{instance_type:<16}[/] ${price.hourly_usd:.4f}/hour  "
                      f"[dim]${price.monthly_usd:,.2f}/month · effective {price.effective_date or '—'}[/]")
    console.print()
    return 0


if __name__ == "__main__":
    sys.exit(main())