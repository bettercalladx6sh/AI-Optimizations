"""On-demand price records shared by every provider's pricing module.

    app/cloud/aws/ec2/pricing.py     AWS Price List Query API
    app/cloud/azure/vm/pricing.py    Azure Retail Prices API

Only prices the provider returns are used; nothing is estimated. Monthly figures use the
730-hour month both AWS and Azure use for estimates (8760 h / 12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

HOURS_PER_MONTH = 730


@dataclass(frozen=True)
class InstancePrice:
    """The on-demand list price of one instance type (EC2) or VM size (Azure)."""

    instance_type: str
    region: str
    operating_system: str
    tenancy: str | None
    hourly_usd: float
    effective_date: str | None
    published_at: str | None

    @property
    def monthly_usd(self) -> float:
        return round(self.hourly_usd * HOURS_PER_MONTH, 2)


@dataclass(frozen=True)
class PricingBasis:
    """What to price an instance as, or why it cannot be priced."""

    operating_system: str | None
    tenancy: str | None
    reason: str | None = None


def fetched_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
