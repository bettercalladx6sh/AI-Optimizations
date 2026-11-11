"""Real-time Azure VM pay-as-you-go prices from the public Azure Retail Prices API.

One request per location and batch of VM sizes (armSkuName filter) returns the current
consumption (pay-as-you-go) list price per hour. Spot and Low Priority meters, reservations
and savings plans are not considered. Results are cached for the run, and requests go through
the shared backoff, so throttling (429) slows the run down instead of failing it.

Only prices Azure returns are used: a size with no single, unambiguous price gets no price,
and nothing is estimated. Azure flags one region of a shared meter as primary
(isPrimaryMeterRegion); the primary meter is preferred, but when a size has no primary meter
in the location (e.g. the DSv3 series, whose only pay-as-you-go meter is not primary) its
non-primary meter is used, so such sizes are still priced.

VMs whose bill depends on more than the size get a reason instead of a price: Spot VMs, VMs
on dedicated hosts, marketplace plans and images that carry a pay-as-you-go software licence
(RHEL, SUSE, SQL Server, Ubuntu Pro). A Windows VM with Azure Hybrid Benefit pays the Linux
(compute only) rate, and a RHEL/SUSE VM with its own subscription (BYOS) the Linux rate.

Usage:
    python -m app.cloud.azure.vm.pricing Standard_D2s_v5 Standard_D4s_v5 --location eastus
    python -m app.cloud.azure.vm.pricing Standard_E4s_v5 --location westeurope --os Windows
"""

from __future__ import annotations

import argparse
import sys
from functools import lru_cache
from typing import Any

from app.auth.common import AuthError, console, hint, status
from app.cloud.azure import rest
from app.cloud.pricing import HOURS_PER_MONTH, InstancePrice, PricingBasis, fetched_at

__all__ = ["HOURS_PER_MONTH", "InstancePrice", "PricingBasis", "fetch_prices", "fetched_at", "pricing_basis",
           "meter_os"]

PRICES_URL = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"
BATCH = 10                              # VM sizes per request (keeps the $filter short)
SOURCE = "Azure Retail Prices API (pay-as-you-go)"

LINUX, WINDOWS = "Linux", "Windows"
HYBRID_BENEFIT = "Windows with Azure Hybrid Benefit"
BYOS = "Linux with own subscription (BYOS)"
METER_OS = {LINUX: LINUX, WINDOWS: WINDOWS, HYBRID_BENEFIT: LINUX, BYOS: LINUX}

HYBRID_LICENCES = {"windows_server", "windows_client"}
BYOS_LICENCES = {"rhel_byos", "sles_byos"}
# Image publishers whose pay-as-you-go images add a software charge to the VM price.
LICENSED_PUBLISHERS = {
    "redhat": "Red Hat Enterprise Linux",
    "suse": "SUSE Linux Enterprise",
    "microsoftsqlserver": "SQL Server",
}
EXCLUDED_METERS = ("spot", "low priority")


def meter_os(basis: PricingBasis) -> str:
    """Which price meter (Linux or Windows) a pricing basis uses."""
    return METER_OS[basis.operating_system or LINUX]


def pricing_basis(configuration: dict[str, Any]) -> PricingBasis:
    """Map a VM configuration to the price it pays; explain when list prices cannot price it."""
    priority = configuration.get("priority") or "Regular"
    if priority != "Regular":
        return PricingBasis(None, None, f"{priority} VM; pay-as-you-go list prices do not apply")
    if configuration.get("dedicated_host"):
        return PricingBasis(None, None, "runs on a dedicated host, which is billed per host, not per VM size")
    if configuration.get("marketplace_plan"):
        return PricingBasis(None, None, f"marketplace plan {configuration['marketplace_plan']} adds software "
                                        "charges that the Retail Prices API does not include")
    licence = (configuration.get("license_type") or "").lower()
    publisher = (configuration.get("image") or "").split(":", 1)[0].lower()
    os_type = configuration.get("os_type")
    if licence == "ubuntu_pro":
        return PricingBasis(None, None, "Ubuntu Pro adds a software charge that is not part of the VM size price")
    if publisher in LICENSED_PUBLISHERS and licence not in BYOS_LICENCES | HYBRID_LICENCES:
        return PricingBasis(None, None, f"{LICENSED_PUBLISHERS[publisher]} pay-as-you-go image adds a software "
                                        "charge that is not part of the VM size price")
    if os_type == WINDOWS:
        return PricingBasis(HYBRID_BENEFIT if licence in HYBRID_LICENCES else WINDOWS, None)
    if os_type == LINUX:
        return PricingBasis(BYOS if licence in BYOS_LICENCES else LINUX, None)
    return PricingBasis(None, None, "the VM's OS type is unknown")


def _filter(location: str, sizes: tuple[str, ...]) -> str:
    names = " or ".join(f"armSkuName eq '{size}'" for size in sizes)
    return (f"serviceName eq 'Virtual Machines' and type eq 'Consumption' and "
            f"armRegionName eq '{location}' and ({names})")


def _matches(item: dict[str, Any], operating_system: str) -> bool:
    """A pay-as-you-go hourly meter for the operating system (no Spot / Low Priority), primary or not."""
    meter = f"{item.get('skuName', '')} {item.get('meterName', '')}".lower()
    windows = item.get("productName", "").endswith(" Windows")
    return (
        item.get("type") == "Consumption"
        and item.get("unitOfMeasure") == "1 Hour"
        and not any(excluded in meter for excluded in EXCLUDED_METERS)
        and windows == (operating_system == WINDOWS)
        and float(item.get("retailPrice") or 0) > 0
    )


@lru_cache(maxsize=128)
def fetch_prices(location: str, sizes: tuple[str, ...], operating_system: str = LINUX) -> dict[str, InstancePrice]:
    """Pay-as-you-go hourly prices for the given VM sizes, keyed by size (missing = not priced).

    `operating_system` is the meter: Linux or Windows.
    """
    location = location.lower()
    found: dict[str, list[tuple[bool, InstancePrice]]] = {}
    wanted = {size.lower(): size for size in sizes}
    ordered = tuple(sorted(wanted.values()))
    for index in range(0, len(ordered), BATCH):
        page = rest.public_get(PRICES_URL, "Azure Retail Prices API",
                               {"api-version": API_VERSION, "$filter": _filter(location, ordered[index:index + BATCH])})
        while True:
            for item in page.get("Items", []):
                size = wanted.get(item.get("armSkuName", "").lower())
                if size is None or not _matches(item, operating_system) or item.get("currencyCode", "USD") != "USD":
                    continue
                primary = bool(item.get("isPrimaryMeterRegion", True))
                found.setdefault(size, []).append((primary, InstancePrice(
                    instance_type=size,
                    region=location,
                    operating_system=operating_system,
                    tenancy=None,
                    hourly_usd=float(item["retailPrice"]),
                    effective_date=item.get("effectiveStartDate"),
                    published_at=None,
                )))
            next_link = page.get("NextPageLink")
            if not next_link:
                break
            page = rest.public_get(next_link, "Azure Retail Prices API")

    # Prefer the primary meter; fall back to non-primary meters only when a size has no primary
    # one. Keep a size only when the chosen meters give one unambiguous price.
    prices: dict[str, InstancePrice] = {}
    for size, meters in found.items():
        chosen = [price for primary, price in meters if primary] or [price for _, price in meters]
        if len({price.hourly_usd for price in chosen}) == 1:
            prices[size] = chosen[0]
    return prices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cloud.azure.vm.pricing",
        description="Show current Azure VM pay-as-you-go list prices from the Azure Retail Prices API.",
    )
    parser.add_argument("sizes", nargs="+", metavar="SIZE", help="VM sizes, e.g. Standard_D2s_v5")
    parser.add_argument("--location", required=True, help="Azure location, e.g. eastus")
    parser.add_argument("--os", default=LINUX, choices=(LINUX, WINDOWS), help="operating system (default: Linux)")
    args = parser.parse_args(argv)

    try:
        prices = fetch_prices(args.location, tuple(args.sizes), args.os)
    except AuthError as exc:
        status(False, f"Pricing failed: {exc}")
        hint(exc.hints)
        return 1

    console.print()
    console.print(f"[bold]Azure VM pay-as-you-go prices[/] [dim]·[/] {args.location} · {args.os}")
    console.print()
    width = max(len(size) for size in args.sizes) + 2
    for size in args.sizes:
        price = prices.get(size)
        if price is None:
            console.print(f"  [yellow]{size:<{width}}[/] [dim]no pay-as-you-go price returned[/]")
            continue
        console.print(f"  [bold]{size:<{width}}[/] ${price.hourly_usd:.4f}/hour  "
                      f"[dim]${price.monthly_usd:,.2f}/month · effective {price.effective_date or '—'}[/]")
    console.print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
