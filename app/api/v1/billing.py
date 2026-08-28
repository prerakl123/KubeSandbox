"""Self-service billing endpoints (doc §13) — everything about their *own* billing a
non-admin caller may see or ask for. Admin review of credit requests, pricing, and
billing-mode changes all live in app/api/v1/admin.py; this router is deliberately the
only billing-adjacent surface a non-admin caller can reach.

Three concerns:

* **The credit/overusage request workflow** — not part of doc §13's original design,
  added on top of it so a tenant blocked by `BillingAuthorizationError` has a path
  forward besides asking an admin out of band.
* **`GET /account`** (Phase 9) — a tenant's own mode, balance, and spend cap. Doc §13
  describes every mechanism that *consumes* these and nothing that reads them back, so
  before this a user hitting a 429 for "insufficient credit" had no way to see their own
  balance, and a UI had nothing to render.
* **`GET /usage`** (Phase 9) — the priced `usage_records` behind that balance, so "where
  did my credit go" is answerable without a DB query.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_billing_service, get_current_principal
from app.api.pagination import Page, PageParamsDep, paginate
from app.core.config import get_settings
from app.persistence.db import get_session
from app.persistence.models import BillingAccount, CreditWallet, UsageRecord
from app.persistence.models import CreditRequest
from app.services.billing_service import BillingService

router = APIRouter(prefix="/v1/billing", tags=["Billing"])


class CreditRequestIn(BaseModel):
    amount: float = Field(
        gt=0,
        description="Additional credit being requested (credit-mode tenants) or the "
        "spend-cap increase being requested (PAYG-mode tenants).",
    )
    reason: str = Field(description="Why more credit/headroom is needed — shown to the reviewing admin.")


class CreditRequestOut(BaseModel):
    id: str
    tenant_id: str
    amount: float
    reason: str
    status: str
    review_note: str | None
    created_at: datetime
    reviewed_at: datetime | None


def credit_request_to_out(row: CreditRequest) -> CreditRequestOut:
    return CreditRequestOut(
        id=row.id,
        tenant_id=row.tenant_id,
        amount=row.amount,
        reason=row.reason,
        status=row.status,
        review_note=row.review_note,
        created_at=row.created_at,
        reviewed_at=row.reviewed_at,
    )


@router.post(
    "/credit-requests",
    response_model=CreditRequestOut,
    summary="Request additional credit or spend-cap headroom",
    description=(
        "Self-service ask for more credit (credit-mode tenants) or a higher spend "
        "cap (PAYG-mode tenants) — typically filed right after hitting a "
        "BillingAuthorizationError (doc §13's 'insufficient credit'/'spend cap "
        "exceeded'). Purely queues a request for admin review "
        "(GET/PATCH /v1/admin/credit-requests) — it never grants anything itself."
    ),
)
async def request_credit(
    body: CreditRequestIn,
    principal: Principal = Depends(get_current_principal),
    billing: BillingService = Depends(get_billing_service),
    session: AsyncSession = Depends(get_session),
) -> CreditRequestOut:
    row = await billing.request_credit(
        principal.tenant_id, body.amount, reason=body.reason, user_id=principal.user_id, session=session
    )
    return credit_request_to_out(row)


@router.get(
    "/credit-requests",
    response_model=list[CreditRequestOut],
    summary="List this tenant's own credit/overusage requests",
    description="Every credit/spend-cap request the caller's own tenant has filed, newest first, with its current review status.",
)
async def list_own_credit_requests(
    principal: Principal = Depends(get_current_principal),
    billing: BillingService = Depends(get_billing_service),
    session: AsyncSession = Depends(get_session),
) -> list[CreditRequestOut]:
    rows = await billing.list_credit_requests(tenant_id=principal.tenant_id, session=session)
    return [credit_request_to_out(row) for row in rows]


class BillingAccountResponse(BaseModel):
    """A tenant's own billing position (doc §13).

    `enabled` first, and separate from everything else: with `billing.enabled` false
    (the default in both env profiles) nothing is ever priced or deducted, and a UI must
    say "billing is not enabled here" rather than render a balance of 0 that looks like
    an empty wallet.
    """

    enabled: bool = Field(description="Whether billing is enforced in this deployment at all.")
    mode: str = Field(description="credit | payg (doc §13). Set per tenant by an admin.")
    currency: str
    spend_cap: float | None = Field(
        description="PAYG only, and advisory (doc §13.2): creation is blocked once this "
        "cycle's usage plus the new estimate would exceed it. Null means unconstrained."
    )
    balance: float | None = Field(
        description="Credit mode only — null for a PAYG tenant, which has no wallet at "
        "all rather than a wallet of zero."
    )
    month_to_date_cost: float = Field(
        description="Priced usage since the start of the current calendar month, which "
        "is what PAYG's spend cap is measured against (see the scope note in the "
        "checklist: the 'billing cycle' is the calendar month, with no cycle-boundary "
        "bookkeeping behind it)."
    )


class UsageRecordResponse(BaseModel):
    id: str
    resource_type: str = Field(description="cpu_second | memory_gb_second | storage_gb_day | db_hour")
    quantity: float
    cost: float
    sandbox_id: str | None
    run_id: str | None
    recorded_at: datetime


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@router.get(
    "/account",
    response_model=BillingAccountResponse,
    summary="This tenant's billing mode, balance, and month-to-date cost",
    description=(
        "Read-only self-service view of doc §13 state. Changing the mode, the spend cap, "
        "or the balance is admin-only (`PATCH /v1/admin/tenants/{id}/billing`, "
        "`POST /v1/admin/tenants/{id}/credit`) — a tenant can see its position and "
        "*request* more headroom, never grant itself any."
    ),
)
async def my_billing_account(
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> BillingAccountResponse:
    settings = get_settings()
    now = datetime.now(UTC)

    account = await session.get(BillingAccount, principal.tenant_id)
    # Read-only, so no get_or_create here: reporting the configured default for a tenant
    # that hasn't been billed yet is honest, and a GET must not create rows as a side
    # effect. BillingService creates the real row on first actual use.
    mode = account.mode if account is not None else settings.billing.default_mode
    currency = account.currency if account is not None else "USD"
    spend_cap = float(account.spend_cap) if account is not None and account.spend_cap is not None else None

    balance: float | None = None
    if mode == "credit":
        wallet = await session.get(CreditWallet, principal.tenant_id)
        balance = float(wallet.balance) if wallet is not None else 0.0

    month_to_date = (
        await session.execute(
            select(func.coalesce(func.sum(UsageRecord.cost), 0)).where(
                UsageRecord.tenant_id == principal.tenant_id,
                UsageRecord.recorded_at >= _month_start(now),
            )
        )
    ).scalar_one()

    return BillingAccountResponse(
        enabled=settings.billing.enabled,
        mode=mode,
        currency=currency,
        spend_cap=spend_cap,
        balance=balance,
        month_to_date_cost=float(month_to_date),
    )


@router.get(
    "/usage",
    response_model=Page[UsageRecordResponse],
    summary="This tenant's priced usage records",
    description=(
        "The `usage_records` rows behind the balance, newest first — what a UI's "
        "cost-breakdown view reads. Both billing modes write here (doc §13's own "
        "\"reporting is uniform regardless of mode\"), so this endpoint doesn't vary by "
        "mode.\n\n"
        "Note that quantities are derived from configured resource *limits* × duration, "
        "not measured cgroup consumption — see the Phase 8 scope boundary in the "
        "checklist. Treat them as billed amounts, not telemetry."
    ),
)
async def my_usage(
    params: PageParamsDep,
    since_days: Annotated[
        int,
        Query(ge=1, le=365, description="Only records from the last N days."),
    ] = 30,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> Page[UsageRecordResponse]:
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    statement = (
        select(UsageRecord)
        .where(UsageRecord.tenant_id == principal.tenant_id, UsageRecord.recorded_at >= cutoff)
        .order_by(UsageRecord.recorded_at.desc(), UsageRecord.id.desc())
    )
    rows, total = await paginate(session, statement, params)
    return Page[UsageRecordResponse](
        items=[
            UsageRecordResponse(
                id=r.id,
                resource_type=r.resource_type,
                quantity=float(r.quantity),
                cost=float(r.cost),
                sandbox_id=r.sandbox_id,
                run_id=r.run_id,
                recorded_at=r.recorded_at,
            )
            for r in rows
        ],
        total=total,
        limit=params.limit,
        offset=params.offset,
    )
