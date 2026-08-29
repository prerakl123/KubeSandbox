from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    Principal,
    get_audit_service,
    get_billing_service,
    get_entitlement_service,
    get_quota_service,
    require_admin,
)
from app.api.pagination import Page, PageParamsDep, paginate
from app.api.v1.billing import CreditRequestOut, credit_request_to_out
from app.core.config import get_settings
from app.persistence.db import get_session
from app.persistence.models import BillingAccount, CreditWallet, PricingRule, Sandbox, Tenant, User
from app.services import audit_service as audit
from app.services.audit_service import AuditService
from app.services.billing_service import BillingService
from app.services.entitlement_service import EntitlementService
from app.services.quota_service import QuotaService

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
    principal: Principal = Depends(require_admin),
    billing: BillingService = Depends(get_billing_service),
    session: AsyncSession = Depends(get_session),
    audit_svc: AuditService = Depends(get_audit_service),
) -> BillingAccountOut:
    account = await billing.set_mode(tenant_id, body.mode, spend_cap=body.spend_cap, session=session)
    # `set_mode` commits, so this is a standalone write rather than a join.
    await audit_svc.record_standalone(
        action=audit.ADMIN_BILLING_MODE,
        principal=principal,
        tenant_id=tenant_id,
        target=tenant_id,
        detail={"mode": body.mode, "spend_cap": body.spend_cap},
    )
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
    principal: Principal = Depends(require_admin),
    billing: BillingService = Depends(get_billing_service),
    session: AsyncSession = Depends(get_session),
    audit_svc: AuditService = Depends(get_audit_service),
) -> CreditWalletOut:
    wallet = await billing.adjust_credit(tenant_id, body.delta, reason=body.reason, session=session)
    # A CreditLedgerEntry already records the balance movement; this records *who* an
    # admin was when they moved it, which the ledger's free-text `reason` does not.
    await audit_svc.record_standalone(
        action=audit.ADMIN_CREDIT_ADJUST,
        principal=principal,
        tenant_id=tenant_id,
        target=tenant_id,
        detail={"delta": body.delta, "reason": body.reason, "balance_after": float(wallet.balance)},
    )
    return CreditWalletOut(tenant_id=wallet.tenant_id, balance=wallet.balance)


@router.get(
    "/pricing-rules",
    response_model=list[PricingRuleOut],
    summary="List pricing rules",
    description=(
        "Every configured rule, newest `effective_from` first. Rules are append-only "
        "(doc §10.1 — multiple rules per resource type coexist so a rate change can be "
        "scheduled), which means the *current* price for a resource type is the newest "
        "rule whose `effective_from` has passed — a POST-only endpoint left an admin no "
        "way to see what pricing is actually in force. Admin-only."
    ),
)
async def list_pricing_rules(
    resource_type: Annotated[
        str | None,
        Query(description="Filter to one resource type, e.g. 'cpu_second'."),
    ] = None,
    _: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[PricingRuleOut]:
    statement = select(PricingRule)
    if resource_type is not None:
        statement = statement.where(PricingRule.resource_type == resource_type)
    rows = (
        (await session.execute(statement.order_by(PricingRule.effective_from.desc(), PricingRule.id.desc())))
        .scalars()
        .all()
    )
    # Not paginated: pricing rules are a handful of operator-authored rows per resource
    # type, bounded by admin action rather than traffic — the same reason the registry
    # listings aren't paginated either (see app/api/pagination.py).
    return [
        PricingRuleOut(
            id=r.id,
            resource_type=r.resource_type,
            unit_cost=float(r.unit_cost),
            currency=r.currency,
            effective_from=r.effective_from,
        )
        for r in rows
    ]


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


# --- Tenant / user administration (Phase 9) ------------------------------------------
# Neither existed before: doc §17's admin surface covers entitlements, publish grants,
# billing mode, and pricing, all of which take a `tenant_id` an admin was expected to
# already know. An admin UI has to be able to *find* a tenant before it can configure
# one, and had no way to.


class TenantOut(BaseModel):
    id: str
    name: str = Field(
        description="Operator-chosen name, or 'oidc:<directory-id>' for a tenant "
        "provisioned automatically on a user's first OIDC login (doc §11)."
    )
    created_at: datetime
    user_count: int
    active_sandbox_count: int = Field(
        description="Sandboxes not in a terminated state — the number that matters for "
        "'is this tenant actually using the platform', and for spotting a tenant leaking "
        "sandboxes before its TTLs reap them."
    )
    billing_mode: str | None = Field(description="Null until the tenant has a BillingAccount row.")
    credit_balance: float | None = Field(description="Credit-mode tenants only.")


class UserOut(BaseModel):
    id: str
    tenant_id: str
    email: str
    role: str = Field(description="admin | operator | user (doc §11's RBAC).")
    created_at: datetime


class SetUserRoleIn(BaseModel):
    role: Literal["admin", "operator", "user"] = Field(
        description="New role. Promotion to admin is deliberately an explicit admin "
        "action — no OIDC claim can grant it (see AuthService)."
    )


@router.get(
    "/tenants",
    response_model=Page[TenantOut],
    summary="List tenants",
    description=(
        "Every tenant, newest first, with the counts and billing position an admin needs "
        "to decide what to configure. Admin-only, and deliberately not entitlement- "
        "filtered — doc §3.6: admin endpoints bypass entitlement filtering entirely."
    ),
)
async def list_tenants(
    params: PageParamsDep,
    name: Annotated[str | None, Query(description="Case-insensitive substring match on name.")] = None,
    _: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Page[TenantOut]:
    statement = select(Tenant)
    if name:
        statement = statement.where(Tenant.name.ilike(f"%{name}%"))
    statement = statement.order_by(Tenant.created_at.desc(), Tenant.id.desc())
    rows, total = await paginate(session, statement, params)

    # Per-page aggregate lookups rather than N queries per row: `limit` is capped at
    # 200, so these are three bounded queries regardless of page size, where a
    # per-tenant count would be 3xN.
    tenant_ids = [t.id for t in rows]
    users_by_tenant: dict[str, int] = {}
    sandboxes_by_tenant: dict[str, int] = {}
    accounts: dict[str, str] = {}
    balances: dict[str, float] = {}
    if tenant_ids:
        for tenant_id, count in (
            await session.execute(
                select(User.tenant_id, func.count())
                .where(User.tenant_id.in_(tenant_ids))
                .group_by(User.tenant_id)
            )
        ).all():
            users_by_tenant[tenant_id] = int(count)
        for tenant_id, count in (
            await session.execute(
                select(Sandbox.tenant_id, func.count())
                .where(Sandbox.tenant_id.in_(tenant_ids), Sandbox.state != "terminated")
                .group_by(Sandbox.tenant_id)
            )
        ).all():
            sandboxes_by_tenant[tenant_id] = int(count)
        for account in (
            await session.execute(select(BillingAccount).where(BillingAccount.tenant_id.in_(tenant_ids)))
        ).scalars():
            accounts[account.tenant_id] = account.mode
        for wallet in (
            await session.execute(select(CreditWallet).where(CreditWallet.tenant_id.in_(tenant_ids)))
        ).scalars():
            balances[wallet.tenant_id] = float(wallet.balance)

    return Page[TenantOut](
        items=[
            TenantOut(
                id=t.id,
                name=t.name,
                created_at=t.created_at,
                user_count=users_by_tenant.get(t.id, 0),
                active_sandbox_count=sandboxes_by_tenant.get(t.id, 0),
                billing_mode=accounts.get(t.id),
                credit_balance=balances.get(t.id),
            )
            for t in rows
        ],
        total=total,
        limit=params.limit,
        offset=params.offset,
    )


@router.get(
    "/users",
    response_model=Page[UserOut],
    summary="List users",
    description="Every user, newest first, optionally scoped to one tenant. Admin-only.",
)
async def list_users(
    params: PageParamsDep,
    tenant_id: Annotated[str | None, Query(description="Only users in this tenant.")] = None,
    email: Annotated[str | None, Query(description="Case-insensitive substring match on email.")] = None,
    _: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Page[UserOut]:
    statement = select(User)
    if tenant_id is not None:
        statement = statement.where(User.tenant_id == tenant_id)
    if email:
        statement = statement.where(User.email.ilike(f"%{email}%"))
    statement = statement.order_by(User.created_at.desc(), User.id.desc())
    rows, total = await paginate(session, statement, params)
    return Page[UserOut](
        items=[
            UserOut(id=u.id, tenant_id=u.tenant_id, email=u.email, role=u.role, created_at=u.created_at)
            for u in rows
        ],
        total=total,
        limit=params.limit,
        offset=params.offset,
    )


@router.patch(
    "/users/{user_id}/role",
    response_model=UserOut,
    summary="Change a user's role",
    description=(
        "The only way to promote someone to `admin` (doc §11's RBAC). Deliberately an "
        "explicit admin action rather than something derived from an IdP claim — "
        "`AuthService` never grants a role from a token, so without this endpoint the "
        "first admin can only be created by a direct DB write."
    ),
    responses={404: {"description": "No such user."}},
)
async def set_user_role(
    body: SetUserRoleIn,
    user_id: str = Path(description="User id, as returned by GET /v1/admin/users."),
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    audit_svc: AuditService = Depends(get_audit_service),
) -> UserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such user: {user_id}")
    previous = user.role
    user.role = body.role
    # The single most consequential mutation in this API — it can grant `admin`. Records
    # both the previous and new role, so a privilege escalation is visible as a
    # transition rather than only as a final state.
    audit_svc.record(
        session,
        action=audit.ADMIN_ROLE_CHANGE,
        principal=principal,
        tenant_id=user.tenant_id,
        target=user.id,
        detail={"email": user.email, "previous_role": previous, "new_role": body.role},
    )
    await session.commit()
    await session.refresh(user)
    return UserOut(
        id=user.id, tenant_id=user.tenant_id, email=user.email, role=user.role, created_at=user.created_at
    )


# --- Quotas (doc §11, doc §10.1's `quotas` table) ------------------------------------


class QuotaOut(BaseModel):
    """A tenant's ceilings and current position against each. Null = no limit."""

    tenant_id: str
    enabled: bool = Field(
        description="Whether quota enforcement is on in this deployment at all. False "
        "means the limits below are recorded but not applied — an admin configuring them "
        "against a deployment with `quota.enabled: false` should be told."
    )
    max_concurrent_sandboxes: int | None
    max_cpu_millicores: int | None
    max_memory_mb: int | None
    max_monthly_minutes: int | None
    concurrent_sandboxes: int = Field(description="Live (non-terminated) sandboxes right now.")
    cpu_millicores: int = Field(
        description="Summed across live sandboxes from per-weight-class budgets, not "
        "measured consumption — see QuotaService's own notes on the approximation."
    )
    memory_mb: int
    monthly_minutes: int = Field(description="Sandbox compute minutes this calendar month.")


class SetQuotaIn(BaseModel):
    max_concurrent_sandboxes: int | None = Field(default=None, ge=0)
    max_cpu_millicores: int | None = Field(default=None, ge=0)
    max_memory_mb: int | None = Field(default=None, ge=0)
    max_monthly_minutes: int | None = Field(default=None, ge=0)
    clear_unset: bool = Field(
        default=False,
        description="False (default) leaves omitted dimensions untouched — a PATCH. True "
        "treats an omitted dimension as 'no limit' — a PUT. Without this an admin has no "
        "way to *remove* a cap, since 'unset' and 'unlimited' would be indistinguishable.",
    )


async def _quota_out(
    tenant_id: str, quota_service: QuotaService, session: AsyncSession
) -> QuotaOut:
    usage = await quota_service.usage(tenant_id, session=session)
    await session.commit()  # persists the lazily-created row, if this was the first look
    return QuotaOut(
        tenant_id=tenant_id,
        enabled=get_settings().quota.enabled,
        max_concurrent_sandboxes=usage.max_concurrent_sandboxes,
        max_cpu_millicores=usage.max_cpu_millicores,
        max_memory_mb=usage.max_memory_mb,
        max_monthly_minutes=usage.max_monthly_minutes,
        concurrent_sandboxes=usage.concurrent_sandboxes,
        cpu_millicores=usage.cpu_millicores,
        memory_mb=usage.memory_mb,
        monthly_minutes=usage.monthly_minutes,
    )


@router.get(
    "/tenants/{tenant_id}/quota",
    response_model=QuotaOut,
    summary="Get a tenant's quotas and current usage",
    description=(
        "Ceilings plus live usage against each (doc §11). The row is created lazily from "
        "the configured defaults on first read, so a tenant that has never been looked at "
        "reports the defaults rather than 404ing."
    ),
)
async def get_tenant_quota(
    tenant_id: str = Path(description="Tenant id, as returned by GET /v1/admin/tenants."),
    _: Principal = Depends(require_admin),
    quota_service: QuotaService = Depends(get_quota_service),
    session: AsyncSession = Depends(get_session),
) -> QuotaOut:
    return await _quota_out(tenant_id, quota_service, session)


@router.patch(
    "/tenants/{tenant_id}/quota",
    response_model=QuotaOut,
    summary="Set a tenant's quotas",
    description=(
        "Admin-only. Quotas answer a different question from billing: billing asks whether "
        "a tenant can *afford* something, quotas whether it should be *allowed* that much "
        "at once — so a funded tenant is still bounded, and a tenant with billing disabled "
        "is bounded at all."
    ),
)
async def set_tenant_quota(
    body: SetQuotaIn,
    tenant_id: str = Path(description="Tenant id."),
    principal: Principal = Depends(require_admin),
    quota_service: QuotaService = Depends(get_quota_service),
    audit_svc: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_session),
) -> QuotaOut:
    await quota_service.set_quota(
        tenant_id,
        session=session,
        max_concurrent_sandboxes=body.max_concurrent_sandboxes,
        max_cpu_millicores=body.max_cpu_millicores,
        max_memory_mb=body.max_memory_mb,
        max_monthly_minutes=body.max_monthly_minutes,
        clear_unset=body.clear_unset,
    )
    audit_svc.record(
        session,
        action=audit.ADMIN_QUOTA_CHANGE,
        principal=principal,
        tenant_id=tenant_id,
        target=tenant_id,
        detail=body.model_dump(),
    )
    await session.commit()
    return await _quota_out(tenant_id, quota_service, session)


# --- Audit log (doc §6 Layer 5) -------------------------------------------------------


class AuditEntryOut(BaseModel):
    id: str
    tenant_id: str | None
    actor: str = Field(
        description="User id, `service:<tenant_id>` for an API-key caller (a key "
        "authenticates as a tenant, not a person), or `system` for the reconciler."
    )
    action: str = Field(description="Dotted `subject.verb`, e.g. 'sandbox.run', 'denied.quota'.")
    target: str | None
    detail: dict | None = Field(
        description="Identifiers, counts, and outcomes only — never code, stdin, stdout, "
        "or credentials."
    )
    created_at: datetime


@router.get(
    "/audit-logs",
    response_model=Page[AuditEntryOut],
    summary="Query the audit trail",
    description=(
        "Doc §6 Layer 5's audit log, newest first. Admin-only and deliberately not "
        "tenant-scoped to the caller: an admin investigating an incident needs to see "
        "across tenants, and `tenant_id` is available as a filter for when they don't.\n\n"
        "This is the *queryable* copy. Doc §6's tamper-resistance intent is served by the "
        "shipped copy — structured logs on stdout, collected off-host — since anyone who "
        "can reach this database can also edit these rows."
    ),
)
async def list_audit_logs(
    params: PageParamsDep,
    tenant_id: Annotated[str | None, Query(description="Only this tenant's entries.")] = None,
    actor: Annotated[str | None, Query(description="Exact actor match.")] = None,
    action: Annotated[str | None, Query(description="Exact action match, e.g. 'sandbox.destroy'.")] = None,
    target: Annotated[str | None, Query(description="Exact target match, e.g. a sandbox id.")] = None,
    _: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Page[AuditEntryOut]:
    statement = AuditService.query(tenant_id=tenant_id, actor=actor, action=action, target=target)
    rows, total = await paginate(session, statement, params)
    return Page[AuditEntryOut](
        items=[
            AuditEntryOut(
                id=r.id,
                tenant_id=r.tenant_id,
                actor=r.actor,
                action=r.action,
                target=r.target,
                detail=r.detail,
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=total,
        limit=params.limit,
        offset=params.offset,
    )
