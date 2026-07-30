"""Billing/costing domain types (doc §13, Phase 8) — mirrors the doc's own
`CostingStrategy` Protocol, extended with an explicit `session` keyword (every other
service in this codebase threads a caller-supplied `AsyncSession` rather than owning
one, e.g. WorkspaceService/EntitlementService) and an `account` keyword (the resolved
`BillingAccount` row, so a strategy never has to re-query it).

Distinct from app/persistence/models.py's billing tables (the durable rows) the same
way app/domain/execution.py is distinct from the ORM — these are the request-scoped
values services pass around.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.persistence.models import BillingAccount, Invoice


@dataclass
class UsageEstimate:
    """A pre-authorization ceiling (doc §13: "Sandbox creation is pre-authorized
    against the wallet balance... before any resource is provisioned") — resource_type
    -> quantity, priced against the latest applicable `PricingRule` per type."""

    quantities: dict[str, float] = field(default_factory=dict)


@dataclass
class UsageEvent:
    """One actually-incurred unit of usage (doc §13/§10.1) — resource_type/quantity,
    optionally tied back to the sandbox/run that incurred it."""

    resource_type: str
    quantity: float
    sandbox_id: str | None = None
    run_id: str | None = None


@dataclass
class AuthResult:
    authorized: bool
    reason: str | None = None
    estimated_cost: float = 0.0


@dataclass
class BillingPeriod:
    start: datetime
    end: datetime


class CostingStrategy(Protocol):
    async def authorize(
        self, tenant_id: str, estimate: UsageEstimate, *, account: "BillingAccount", session: AsyncSession
    ) -> AuthResult: ...

    async def record_usage(
        self, tenant_id: str, event: UsageEvent, *, account: "BillingAccount", session: AsyncSession
    ) -> None: ...

    async def settle(self, tenant_id: str, period: BillingPeriod, *, session: AsyncSession) -> "Invoice": ...
