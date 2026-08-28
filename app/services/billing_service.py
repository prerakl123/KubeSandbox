"""BillingService & costing strategies (doc §13, Phase 8).

Two billing modes coexist per tenant, selected via `BillingAccount.mode`:

- **credit** (`CreditBillingStrategy`) — `authorize()` is a hard pre-check against
  `CreditWallet.balance`; insufficient credit blocks sandbox creation before any
  resource is provisioned. `record_usage()` prices the event against `pricing_rules`
  and deducts it from the wallet in real time, with a `CreditLedgerEntry` audit row
  per deduction.
- **payg** (`PayAsYouGoBillingStrategy`) — `authorize()` is advisory: it checks nothing
  but an optional per-tenant `spend_cap` (`None` = unconstrained). The real work is in
  `settle()`: usage accumulated in `usage_records` over a billing period rolls up into
  a draft `Invoice`. Actual payment collection is a deliberate stub everywhere (doc
  §13) — `settle()` only ever produces a `draft` Invoice row.

Both strategies write to the same `usage_records` table so reporting is uniform
regardless of mode (doc's own framing), and both delegate `settle()` to the same
`_generate_invoice_draft` helper rather than duplicating that logic.

Opt-in like `PoolManager`/`WorkspaceService` before it (doc §4.3/§10.2's own
precedent): `BillingService` itself has no "disabled" state — the opt-in happens one
layer up, in `app/api/deps.py::_build_sandbox_service`, which passes
`billing_service=None` to `SandboxService` whenever `settings.billing.enabled` is
false (the default in both `local.yaml`/`aks-prod.yaml`). Every authorize()/
record_usage() call site in `SandboxService` is skipped entirely in that case — zero
behavior change for a deployment that hasn't turned this on.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.errors import KubeSandboxError
from app.core.logging import get_logger
from app.domain.billing import AuthResult, BillingPeriod, UsageEstimate, UsageEvent
from app.domain.execution import ResourceSpec
from app.persistence.models import (
    BillingAccount,
    CreditLedgerEntry,
    CreditRequest,
    CreditWallet,
    Invoice,
    PricingRule,
    UsageRecord,
)
from app.provisioners.resources import parse_cpu_to_nanocpus, parse_memory_to_bytes

logger = get_logger(__name__)


async def _get_or_create_account(tenant_id: str, default_mode: str, session: AsyncSession) -> BillingAccount:
    account = await session.get(BillingAccount, tenant_id)
    if account is None:
        account = BillingAccount(tenant_id=tenant_id, mode=default_mode)
        session.add(account)
        await session.flush()
    return account


async def _latest_unit_cost(resource_type: str, session: AsyncSession, *, now: datetime) -> float:
    """Picks the latest `PricingRule` whose `effective_from` has arrived — multiple
    rules for the same resource_type coexist by design (doc §10.1), the newest
    already-effective one wins. No rule at all -> priced as free, logged rather than
    raised: an admin who hasn't configured pricing yet shouldn't have every sandbox
    creation start failing with an opaque error."""
    stmt = (
        select(PricingRule.unit_cost)
        .where(PricingRule.resource_type == resource_type, PricingRule.effective_from <= now)
        .order_by(PricingRule.effective_from.desc())
        .limit(1)
    )
    unit_cost = (await session.execute(stmt)).scalar_one_or_none()
    if unit_cost is None:
        logger.warning("no_pricing_rule_for_resource_type", resource_type=resource_type)
        return 0.0
    return float(unit_cost)


async def _price_estimate(estimate: UsageEstimate, session: AsyncSession, *, now: datetime) -> float:
    total = 0.0
    for resource_type, quantity in estimate.quantities.items():
        total += quantity * await _latest_unit_cost(resource_type, session, now=now)
    return total


async def _period_usage_cost(tenant_id: str, session: AsyncSession, *, since: datetime, until: datetime) -> float:
    stmt = select(func.coalesce(func.sum(UsageRecord.cost), 0)).where(
        UsageRecord.tenant_id == tenant_id,
        UsageRecord.recorded_at >= since,
        UsageRecord.recorded_at < until,
    )
    return float((await session.execute(stmt)).scalar_one())


def _current_cycle_start(now: datetime) -> datetime:
    """PAYG's spend-cap window (doc §13.2's "billing cycle") — the calendar month to
    date. No `Invoice`/cycle-boundary bookkeeping exists yet to anchor this more
    precisely; calendar-month is a reasonable, simple default an admin can't
    misconfigure."""
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _record_usage_row(tenant_id: str, event: UsageEvent, cost: float, session: AsyncSession) -> UsageRecord:
    row = UsageRecord(
        tenant_id=tenant_id,
        sandbox_id=event.sandbox_id,
        run_id=event.run_id,
        resource_type=event.resource_type,
        quantity=event.quantity,
        cost=cost,
    )
    session.add(row)
    await session.flush()
    return row


async def _generate_invoice_draft(tenant_id: str, period: BillingPeriod, session: AsyncSession) -> Invoice:
    total_cost = await _period_usage_cost(tenant_id, session, since=period.start, until=period.end)
    invoice = Invoice(
        tenant_id=tenant_id,
        period_start=period.start,
        period_end=period.end,
        total_cost=total_cost,
        status="draft",
    )
    session.add(invoice)
    await session.flush()
    return invoice


class CreditBillingStrategy:
    async def authorize(self, tenant_id, estimate, *, account, session) -> AuthResult:
        now = datetime.now(UTC)
        cost = await _price_estimate(estimate, session, now=now)
        wallet = await session.get(CreditWallet, tenant_id)
        balance = float(wallet.balance) if wallet is not None else 0.0
        if cost > balance:
            return AuthResult(
                authorized=False,
                reason=f"insufficient credit: estimated cost {cost:.4f} exceeds balance {balance:.4f}",
                estimated_cost=cost,
            )
        return AuthResult(authorized=True, estimated_cost=cost)

    async def record_usage(self, tenant_id, event, *, account, session) -> None:
        now = datetime.now(UTC)
        cost = event.quantity * await _latest_unit_cost(event.resource_type, session, now=now)
        await _record_usage_row(tenant_id, event, cost, session)

        wallet = await session.get(CreditWallet, tenant_id)
        if wallet is None:
            wallet = CreditWallet(tenant_id=tenant_id, balance=0)
            session.add(wallet)
            await session.flush()
        wallet.balance = float(wallet.balance) - cost
        session.add(
            CreditLedgerEntry(
                tenant_id=tenant_id,
                delta=-cost,
                reason=f"usage:{event.resource_type}:{event.sandbox_id or ''}",
                balance_after=wallet.balance,
            )
        )
        # Doc §14's "quota/credit usage". Recorded here rather than at the
        # BillingService facade because this is where the priced `cost` actually
        # exists — the facade only ever sees a `None` return. The balance gauge is
        # deliberately last-write-wins per tenant rather than a counter: a balance is
        # a level, and it moves in both directions (top-ups via adjust_credit()).
        metrics.usage_cost_total.labels(resource_type=event.resource_type, mode="credit").inc(cost)
        metrics.credit_balance.labels(tenant_id=tenant_id).set(wallet.balance)

    async def settle(self, tenant_id, period, *, session) -> Invoice:
        # Credit tenants already pay in real time via record_usage()'s wallet
        # deduction — this draft invoice is a reporting artifact only (doc's own
        # "both strategies write to usage_records so reporting is uniform"), not a
        # bill that's actually owed on top of what the wallet already paid.
        return await _generate_invoice_draft(tenant_id, period, session)


class PayAsYouGoBillingStrategy:
    async def authorize(self, tenant_id, estimate, *, account, session) -> AuthResult:
        now = datetime.now(UTC)
        cost = await _price_estimate(estimate, session, now=now)
        if account.spend_cap is None:
            return AuthResult(authorized=True, estimated_cost=cost)
        cycle_start = _current_cycle_start(now)
        spent = await _period_usage_cost(tenant_id, session, since=cycle_start, until=now)
        spend_cap = float(account.spend_cap)
        if spent + cost > spend_cap:
            return AuthResult(
                authorized=False,
                reason=(
                    f"spend cap exceeded: {spent:.4f} already spent this cycle + "
                    f"{cost:.4f} estimated > cap {spend_cap:.4f}"
                ),
                estimated_cost=cost,
            )
        return AuthResult(authorized=True, estimated_cost=cost)

    async def record_usage(self, tenant_id, event, *, account, session) -> None:
        now = datetime.now(UTC)
        cost = event.quantity * await _latest_unit_cost(event.resource_type, session, now=now)
        await _record_usage_row(tenant_id, event, cost, session)
        # No `credit_balance` counterpart here: a PAYG tenant has no wallet at all
        # (see this class's own authorize() — spend cap only), so there is no level to
        # report, only accumulated cost.
        metrics.usage_cost_total.labels(resource_type=event.resource_type, mode="payg").inc(cost)

    async def settle(self, tenant_id, period, *, session) -> Invoice:
        return await _generate_invoice_draft(tenant_id, period, session)


class BillingService:
    def __init__(self, *, default_mode: str = "credit") -> None:
        self._default_mode = default_mode

    @staticmethod
    def _strategy_for(mode: str) -> CreditBillingStrategy | PayAsYouGoBillingStrategy:
        if mode == "credit":
            return CreditBillingStrategy()
        if mode == "payg":
            return PayAsYouGoBillingStrategy()
        raise KubeSandboxError(f"unknown billing mode: {mode!r}")

    async def authorize(self, tenant_id: str, estimate: UsageEstimate, *, session: AsyncSession) -> AuthResult:
        account = await _get_or_create_account(tenant_id, self._default_mode, session)
        result = await self._strategy_for(account.mode).authorize(
            tenant_id, estimate, account=account, session=session
        )
        if not result.authorized:
            # Counted at the facade, not in either strategy: a denial is a denial
            # regardless of which rule produced it (balance vs. spend cap), and the
            # `mode` label already carries that distinction.
            metrics.billing_denials_total.labels(mode=account.mode).inc()
        return result

    async def record_usage(self, tenant_id: str, event: UsageEvent, *, session: AsyncSession) -> None:
        account = await _get_or_create_account(tenant_id, self._default_mode, session)
        await self._strategy_for(account.mode).record_usage(tenant_id, event, account=account, session=session)

    async def settle(self, tenant_id: str, period: BillingPeriod, *, session: AsyncSession) -> Invoice:
        account = await _get_or_create_account(tenant_id, self._default_mode, session)
        return await self._strategy_for(account.mode).settle(tenant_id, period, session=session)

    async def set_mode(
        self, tenant_id: str, mode: str, *, spend_cap: float | None, session: AsyncSession
    ) -> BillingAccount:
        if mode not in ("credit", "payg"):
            raise KubeSandboxError(f"mode must be 'credit' or 'payg', got {mode!r}")
        account = await _get_or_create_account(tenant_id, self._default_mode, session)
        account.mode = mode
        account.spend_cap = spend_cap
        await session.commit()
        await session.refresh(account)
        return account

    async def adjust_credit(
        self, tenant_id: str, delta: float, *, reason: str = "admin_adjustment", session: AsyncSession
    ) -> CreditWallet:
        """The only way to fund (or manually correct) a credit-mode tenant's wallet
        today — there is no external payment-gateway integration (doc §13's own
        deliberate stub) and no automatic top-up path. `delta` may be negative for a
        manual deduction/correction; every call writes a `CreditLedgerEntry` audit row
        regardless of sign, same shape `record_usage()`'s own deductions use."""
        wallet = await session.get(CreditWallet, tenant_id)
        if wallet is None:
            wallet = CreditWallet(tenant_id=tenant_id, balance=0)
            session.add(wallet)
            await session.flush()
        delta = float(delta)  # defensive: a caller may pass a Numeric-column Decimal (e.g. CreditRequest.amount)
        wallet.balance = float(wallet.balance) + delta
        session.add(
            CreditLedgerEntry(tenant_id=tenant_id, delta=delta, reason=reason, balance_after=wallet.balance)
        )
        await session.commit()
        await session.refresh(wallet)
        metrics.credit_balance.labels(tenant_id=tenant_id).set(float(wallet.balance))
        return wallet

    async def request_credit(
        self, tenant_id: str, amount: float, *, reason: str, user_id: str | None, session: AsyncSession
    ) -> CreditRequest:
        """A tenant's self-service ask for more credit (credit mode) or spend-cap
        headroom (PAYG mode) — not part of doc §13's original design, added so a
        tenant that just hit `BillingAuthorizationError` has a real path forward
        besides asking an admin out of band. Purely a queued request; it never grants
        anything itself — see `review_credit_request()` for the approval path."""
        if amount <= 0:
            raise KubeSandboxError(f"requested amount must be positive, got {amount!r}")
        request = CreditRequest(tenant_id=tenant_id, user_id=user_id, amount=amount, reason=reason)
        session.add(request)
        await session.commit()
        await session.refresh(request)
        return request

    async def list_credit_requests(
        self, *, tenant_id: str | None = None, status: str | None = None, session: AsyncSession
    ) -> list[CreditRequest]:
        stmt = select(CreditRequest).order_by(CreditRequest.created_at.desc())
        if tenant_id is not None:
            stmt = stmt.where(CreditRequest.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(CreditRequest.status == status)
        return list((await session.execute(stmt)).scalars().all())

    async def review_credit_request(
        self,
        request_id: str,
        *,
        approve: bool,
        reviewer: str | None,
        note: str | None,
        session: AsyncSession,
    ) -> CreditRequest:
        """Approving applies `request.amount` immediately: `adjust_credit()` for a
        credit-mode tenant, or a `spend_cap` increase for a PAYG-mode tenant (its
        closest analog to "more headroom" — PAYG tenants have no wallet). The
        financial effect is applied (and committed) *before* the request itself is
        marked approved, so a failure applying it leaves the request `pending`
        rather than incorrectly recorded as approved."""
        request = await session.get(CreditRequest, request_id)
        if request is None:
            raise KubeSandboxError(f"no such credit request: {request_id}")
        if request.status != "pending":
            raise KubeSandboxError(f"credit request {request_id} is already {request.status!r}")

        if approve:
            account = await _get_or_create_account(request.tenant_id, self._default_mode, session)
            if account.mode == "credit":
                await self.adjust_credit(
                    request.tenant_id,
                    float(request.amount),
                    reason=f"credit_request:{request.id}",
                    session=session,
                )
            else:
                new_cap = float(account.spend_cap or 0.0) + float(request.amount)
                await self.set_mode(request.tenant_id, account.mode, spend_cap=new_cap, session=session)

        request.status = "approved" if approve else "denied"
        request.review_note = note
        request.reviewed_by = reviewer
        request.reviewed_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(request)
        return request

    async def add_pricing_rule(
        self,
        resource_type: str,
        unit_cost: float,
        *,
        currency: str = "USD",
        effective_from: datetime | None = None,
        session: AsyncSession,
    ) -> PricingRule:
        rule = PricingRule(
            resource_type=resource_type,
            unit_cost=unit_cost,
            currency=currency,
            effective_from=effective_from or datetime.now(UTC),
        )
        session.add(rule)
        await session.commit()
        await session.refresh(rule)
        return rule


# -- ResourceSpec -> usage quantities --------------------------------------------------
# Priced by configured resource *limits* x duration, not real per-container cgroup
# measurement — no metrics pipeline exists yet to measure actual consumption (that's
# Prometheus wiring, roadmap Phase 9). Same "honest, not literal telemetry" flag Phase
# 5 already gave `maxDbSizeMB`. Takes a bare `ResourceSpec` (not a full `SandboxSpec`)
# since that's all either helper actually reads — callers that only have a
# re-resolved spec's resources (e.g. `SandboxService._spec_resources_for_row`, which
# has no image/command/etc. to reconstruct for an already-persisted sandbox) don't
# need to fabricate the rest of a `SandboxSpec` just to call these.

def db_sidecar_count(sidecar_components) -> int:
    """How many of a resolved spec's sidecar components are database add-ons (doc
    §3.3's `access.database`) — the multiplier for the `db_hour` resource type. Shared
    by every usage-computation call site so they never compute it differently."""
    return sum(1 for c in sidecar_components if c.spec.access.database is not None)


def estimate_usage_for_spec(resources: ResourceSpec, seconds: int, *, db_sidecar_count: int = 0) -> UsageEstimate:
    """Pre-authorization ceiling (doc §13) — `seconds` should be the run's wall-clock
    cap (`execute()`) or the sandbox's `max_ttl_seconds` (`create_sandbox()`, since a
    warm sandbox's actual lifetime isn't known upfront but the reconciler guarantees
    it won't outlive that TTL)."""
    cpu_cores = parse_cpu_to_nanocpus(resources.cpu) / 1_000_000_000
    memory_gb = parse_memory_to_bytes(resources.memory) / 1024**3
    quantities = {
        "cpu_second": cpu_cores * seconds,
        "memory_gb_second": memory_gb * seconds,
    }
    if db_sidecar_count:
        quantities["db_hour"] = db_sidecar_count * (seconds / 3600)
    return UsageEstimate(quantities=quantities)


def usage_events_for_run(
    resources: ResourceSpec,
    duration_ms: int,
    *,
    sandbox_id: str,
    run_id: str | None,
    db_sidecar_count: int = 0,
) -> list[UsageEvent]:
    """Actual usage (doc §13) for one completed run — either a single `execute()` call
    (`duration_ms` = that run's real duration) or a whole non-ephemeral sandbox's
    lifetime (`duration_ms` = `terminated_at - created_at`, from `destroy_sandbox()`,
    including a TTL reap — the reconciler's `reap_expired_sandboxes()` already routes
    through `destroy_sandbox()`, so this needs no separate wiring there). Same
    resource-limit x duration pricing basis as `estimate_usage_for_spec`, but against
    real elapsed time instead of a ceiling."""
    seconds = duration_ms / 1000
    cpu_cores = parse_cpu_to_nanocpus(resources.cpu) / 1_000_000_000
    memory_gb = parse_memory_to_bytes(resources.memory) / 1024**3
    events = [
        UsageEvent(resource_type="cpu_second", quantity=cpu_cores * seconds, sandbox_id=sandbox_id, run_id=run_id),
        UsageEvent(
            resource_type="memory_gb_second", quantity=memory_gb * seconds, sandbox_id=sandbox_id, run_id=run_id
        ),
    ]
    if db_sidecar_count:
        events.append(
            UsageEvent(
                resource_type="db_hour",
                quantity=db_sidecar_count * (seconds / 3600),
                sandbox_id=sandbox_id,
                run_id=run_id,
            )
        )
    return events
