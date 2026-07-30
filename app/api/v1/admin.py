from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_billing_service, get_entitlement_service, require_admin
from app.api.v1.billing import CreditRequestOut, credit_request_to_out
from app.persistence.db import get_session
from app.services.billing_service import BillingService
from app.services.entitlement_service import EntitlementService

router = APIRouter(prefix="/v1/admin", tags=["Admin"])


class EntitlementIn(BaseModel):
    scope: str = Field(description="'tenant' or 'user'.")
    scope_id: str = Field(description="The tenant id or user id this entitlement applies to.")
    component_name: str = Field(description="Bare component name (not versioned) this entitlement covers.")
    version_range: str = Field(default="*", description="'*' (any version) or an exact version string.")
    visible: bool = Field(default=True, description="Whether this scope may see/select the component.")


class EntitlementOut(EntitlementIn):
    id: str


class PublishGrantIn(BaseModel):
    scope: str = Field(description="'tenant' or 'user'.")
    scope_id: str = Field(description="The tenant id or user id this grant applies to.")
    category: str = Field(description="One of the Component categories, or 'template' (doc §3.6).")
    allowed: bool = Field(default=True, description="Whether this scope may publish its own private components/templates in this category.")


class PublishGrantOut(PublishGrantIn):
    id: str


@router.get(
    "/entitlements",
    response_model=list[EntitlementOut],
    summary="List catalog entitlements",
    description="What a tenant/user scope may see and select from the component catalog (doc §3.6). Admin-only.",
)
async def list_entitlements(
    scope: str | None = Query(default=None, description="Filter to 'tenant' or 'user' scoped entries."),
    scope_id: str | None = Query(default=None, description="Filter to one tenant id or user id."),
    component_name: str | None = Query(default=None, description="Filter to one component name."),
    _: Principal = Depends(require_admin),
    service: EntitlementService = Depends(get_entitlement_service),
) -> list[EntitlementOut]:
    rows = await service.list_entitlements(
        scope=scope, scope_id=scope_id, component_name=component_name
    )
    return [
        EntitlementOut(
            id=row.id,
            scope=row.scope,
            scope_id=row.scope_id,
            component_name=row.component_name,
            version_range=row.version_range,
            visible=row.visible,
        )
        for row in rows
    ]


@router.patch(
    "/entitlements",
    response_model=EntitlementOut,
    summary="Set a catalog entitlement",
    description="Create or update what a tenant/user scope may see and select (doc §3.6). Admin-only.",
)
async def upsert_entitlement(
    body: EntitlementIn,
    _: Principal = Depends(require_admin),
    service: EntitlementService = Depends(get_entitlement_service),
) -> EntitlementOut:
    row = await service.upsert_entitlement(
        scope=body.scope,
        scope_id=body.scope_id,
        component_name=body.component_name,
        version_range=body.version_range,
        visible=body.visible,
    )
    return EntitlementOut(
        id=row.id,
        scope=row.scope,
        scope_id=row.scope_id,
        component_name=row.component_name,
        version_range=row.version_range,
        visible=row.visible,
    )


@router.get(
    "/publish-grants",
    response_model=list[PublishGrantOut],
    summary="List publish grants",
    description="Who may publish their own private components/templates, and in which category (doc §3.6). Admin-only.",
)
async def list_publish_grants(
    scope: str | None = Query(default=None, description="Filter to 'tenant' or 'user' scoped entries."),
    scope_id: str | None = Query(default=None, description="Filter to one tenant id or user id."),
    category: str | None = Query(default=None, description="Filter to one category, or 'template'."),
    _: Principal = Depends(require_admin),
    service: EntitlementService = Depends(get_entitlement_service),
) -> list[PublishGrantOut]:
    rows = await service.list_publish_grants(scope=scope, scope_id=scope_id, category=category)
    return [
        PublishGrantOut(
            id=row.id, scope=row.scope, scope_id=row.scope_id, category=row.category,
            allowed=row.allowed,
        )
        for row in rows
    ]


@router.patch(
    "/publish-grants",
    response_model=PublishGrantOut,
    summary="Set a publish grant",
    description="Create or update whether a tenant/user scope may publish its own private components/templates in a category (doc §3.6). Admin-only.",
)
async def upsert_publish_grant(
    body: PublishGrantIn,
    _: Principal = Depends(require_admin),
    service: EntitlementService = Depends(get_entitlement_service),
) -> PublishGrantOut:
    row = await service.upsert_publish_grant(
        scope=body.scope, scope_id=body.scope_id, category=body.category, allowed=body.allowed
    )
    return PublishGrantOut(
        id=row.id, scope=row.scope, scope_id=row.scope_id, category=row.category,
        allowed=row.allowed,
    )


# --- Billing (doc §13) ---------------------------------------------------------------

class SetBillingModeRequest(BaseModel):
    mode: Literal["credit", "payg"] = Field(
        description="'credit' (hard pre-authorization against a wallet balance) or "
        "'payg' (advisory spend-cap only; usage rolls up into a draft invoice on settle)."
    )
    spend_cap: float | None = Field(
        default=None,
        description="Optional soft cap (doc §13.2), meaningful for 'payg'; null clears it. "
        "Ignored by 'credit' mode, which is capped by the wallet balance instead.",
    )


class BillingAccountOut(BaseModel):
    tenant_id: str
    mode: str
    spend_cap: float | None
    currency: str


class AdjustCreditRequest(BaseModel):
    delta: float = Field(description="Amount to add to the tenant's credit wallet balance; negative to deduct/correct.")
    reason: str = Field(default="admin_adjustment", description="Recorded verbatim on the CreditLedgerEntry audit row.")


class CreditWalletOut(BaseModel):
    tenant_id: str
    balance: float


class PricingRuleIn(BaseModel):
    resource_type: str = Field(description="e.g. 'cpu_second', 'memory_gb_second', 'storage_gb_day', 'db_hour'.")
    unit_cost: float = Field(description="Cost per unit of `resource_type`, in `currency`.")
    currency: str = Field(default="USD")
    effective_from: datetime | None = Field(
        default=None,
        description="Defaults to now. Multiple rules for the same resource_type may coexist "
        "(doc §10.1) — authorize()/record_usage() always price against the latest "
        "already-effective one, so this lets an admin schedule a future rate change "
        "without disturbing the currently-active rule.",
    )


class PricingRuleOut(BaseModel):
    id: str
    resource_type: str
    unit_cost: float
    currency: str
    effective_from: datetime


@router.patch(
    "/tenants/{tenant_id}/billing",
    response_model=BillingAccountOut,
    summary="Set a tenant's billing mode",
    description=(
        "Switch a tenant between credit and pay-as-you-go billing, and/or set its "
        "spend cap (doc §13). Creates the tenant's billing account (defaulting to "
        "`billing.default_mode`) if it doesn't exist yet. Admin-only."
    ),
)
async def set_tenant_billing(
    body: SetBillingModeRequest,
    tenant_id: str = Path(description="Tenant id."),
    _: Principal = Depends(require_admin),
    billing: BillingService = Depends(get_billing_service),
    session: AsyncSession = Depends(get_session),
) -> BillingAccountOut:
    account = await billing.set_mode(tenant_id, body.mode, spend_cap=body.spend_cap, session=session)
    return BillingAccountOut(
        tenant_id=account.tenant_id, mode=account.mode, spend_cap=account.spend_cap, currency=account.currency,
    )


@router.post(
    "/tenants/{tenant_id}/credit",
    response_model=CreditWalletOut,
    summary="Adjust a tenant's credit wallet balance",
    description=(
        "Top up (positive delta) or deduct/correct (negative delta) a tenant's credit "
        "wallet balance, writing a CreditLedgerEntry audit row per adjustment (doc "
        "§13). The only way to fund a credit-mode tenant's wallet today — real payment "
        "collection is a deliberate stub everywhere in this system (doc §13). "
        "Admin-only."
    ),
)
async def adjust_tenant_credit(
    body: AdjustCreditRequest,
    tenant_id: str = Path(description="Tenant id."),
    _: Principal = Depends(require_admin),
    billing: BillingService = Depends(get_billing_service),
    session: AsyncSession = Depends(get_session),
) -> CreditWalletOut:
    wallet = await billing.adjust_credit(tenant_id, body.delta, reason=body.reason, session=session)
    return CreditWalletOut(tenant_id=wallet.tenant_id, balance=wallet.balance)


@router.post(
    "/pricing-rules",
    response_model=PricingRuleOut,
    summary="Add a pricing rule",
    description=(
        "Configure unit pricing for a resource type (doc §13) — appends a new "
        "versioned rule rather than replacing any existing one for the same "
        "resource_type (doc §10.1). Admin-only."
    ),
)
async def add_pricing_rule(
    body: PricingRuleIn,
    _: Principal = Depends(require_admin),
    billing: BillingService = Depends(get_billing_service),
    session: AsyncSession = Depends(get_session),
) -> PricingRuleOut:
    rule = await billing.add_pricing_rule(
        body.resource_type,
        body.unit_cost,
        currency=body.currency,
        effective_from=body.effective_from,
        session=session,
    )
    return PricingRuleOut(
        id=rule.id,
        resource_type=rule.resource_type,
        unit_cost=rule.unit_cost,
        currency=rule.currency,
        effective_from=rule.effective_from,
    )


class CreditRequestReviewIn(BaseModel):
    approve: bool = Field(
        description="True to approve — immediately applies `amount` as a credit "
        "top-up (credit-mode tenants) or a spend-cap increase (PAYG-mode tenants). "
        "False to deny without applying anything."
    )
    note: str | None = Field(default=None, description="Optional note recorded on the request, e.g. why it was denied.")


@router.get(
    "/credit-requests",
    response_model=list[CreditRequestOut],
    summary="List credit/overusage requests",
    description="Every tenant's credit/spend-cap requests (doc-adjacent, not in §13's original design), optionally filtered by tenant or status. Admin-only.",
)
async def list_credit_requests(
    tenant_id: str | None = Query(default=None, description="Filter to one tenant id."),
    status: str | None = Query(default=None, description="'pending' | 'approved' | 'denied'."),
    _: Principal = Depends(require_admin),
    billing: BillingService = Depends(get_billing_service),
    session: AsyncSession = Depends(get_session),
) -> list[CreditRequestOut]:
    rows = await billing.list_credit_requests(tenant_id=tenant_id, status=status, session=session)
    return [credit_request_to_out(row) for row in rows]


@router.patch(
    "/credit-requests/{request_id}",
    response_model=CreditRequestOut,
    summary="Approve or deny a credit/overusage request",
    description=(
        "Approving immediately applies the requested amount — a wallet top-up "
        "(credit-mode tenants, via the same mechanism as POST .../credit) or a "
        "spend-cap increase (PAYG-mode tenants). Denying just records the review "
        "note. Admin-only."
    ),
)
async def review_credit_request(
    body: CreditRequestReviewIn,
    request_id: str = Path(description="Credit request id."),
    principal: Principal = Depends(require_admin),
    billing: BillingService = Depends(get_billing_service),
    session: AsyncSession = Depends(get_session),
) -> CreditRequestOut:
    row = await billing.review_credit_request(
        request_id,
        approve=body.approve,
        reviewer=principal.user_id,
        note=body.note,
        session=session,
    )
    return credit_request_to_out(row)
