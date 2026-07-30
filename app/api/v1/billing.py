"""Self-service billing endpoints — currently just the credit/overusage request
workflow (not part of doc §13's original design, added on top of it). Admin review of
these requests lives in app/api/v1/admin.py alongside the rest of the billing admin
surface; this router is deliberately the only billing-adjacent surface a non-admin
caller can reach.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_billing_service, get_current_principal
from app.persistence.db import get_session
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
