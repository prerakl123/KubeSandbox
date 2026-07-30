"""BillingService & the two CostingStrategy implementations (doc §13, Phase 8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.errors import KubeSandboxError
from app.domain.billing import BillingPeriod, UsageEstimate, UsageEvent
from app.persistence.models import BillingAccount, CreditLedgerEntry, CreditWallet, PricingRule, UsageRecord
from app.services.billing_service import BillingService


async def _fund_wallet(session, tenant_id: str, balance: float) -> None:
    session.add(CreditWallet(tenant_id=tenant_id, balance=balance))
    await session.flush()


async def _add_rule(session, resource_type: str, unit_cost: float, *, effective_from=None) -> None:
    session.add(
        PricingRule(
            resource_type=resource_type,
            unit_cost=unit_cost,
            effective_from=effective_from or datetime.now(UTC),
        )
    )
    await session.flush()


# -- credit mode -----------------------------------------------------------------------

async def test_credit_authorize_allows_when_balance_covers_estimate(db_session):
    await _fund_wallet(db_session, "t1", balance=100.0)
    await _add_rule(db_session, "cpu_second", 1.0)
    service = BillingService(default_mode="credit")

    result = await service.authorize(
        "t1", UsageEstimate(quantities={"cpu_second": 10.0}), session=db_session
    )

    assert result.authorized is True
    assert result.estimated_cost == 10.0


async def test_credit_authorize_blocks_when_balance_is_insufficient(db_session):
    await _fund_wallet(db_session, "t1", balance=5.0)
    await _add_rule(db_session, "cpu_second", 1.0)
    service = BillingService(default_mode="credit")

    result = await service.authorize(
        "t1", UsageEstimate(quantities={"cpu_second": 10.0}), session=db_session
    )

    assert result.authorized is False
    assert "insufficient credit" in result.reason


async def test_credit_authorize_with_no_wallet_at_all_treats_balance_as_zero(db_session):
    await _add_rule(db_session, "cpu_second", 1.0)
    service = BillingService(default_mode="credit")

    result = await service.authorize(
        "no-wallet-tenant", UsageEstimate(quantities={"cpu_second": 1.0}), session=db_session
    )

    assert result.authorized is False


async def test_credit_record_usage_deducts_wallet_and_writes_ledger_entry(db_session):
    await _fund_wallet(db_session, "t1", balance=100.0)
    await _add_rule(db_session, "cpu_second", 2.0)
    service = BillingService(default_mode="credit")

    await service.record_usage(
        "t1", UsageEvent(resource_type="cpu_second", quantity=10.0, sandbox_id="sb-1"), session=db_session
    )
    await db_session.commit()

    wallet = await db_session.get(CreditWallet, "t1")
    assert wallet.balance == 80.0  # 100 - (10 * 2.0)

    ledger = (await db_session.execute(select(CreditLedgerEntry))).scalars().one()
    assert ledger.delta == -20.0
    assert ledger.balance_after == 80.0

    records = (await db_session.execute(select(UsageRecord))).scalars().all()
    assert len(records) == 1
    assert records[0].sandbox_id == "sb-1"
    assert records[0].cost == 20.0


async def test_credit_record_usage_creates_wallet_when_missing(db_session):
    await _add_rule(db_session, "cpu_second", 1.0)
    service = BillingService(default_mode="credit")

    await service.record_usage(
        "brand-new-tenant", UsageEvent(resource_type="cpu_second", quantity=5.0), session=db_session
    )
    await db_session.commit()

    wallet = await db_session.get(CreditWallet, "brand-new-tenant")
    assert wallet is not None
    assert wallet.balance == -5.0  # went negative rather than raising — authorize() is the gate, not this


# -- payg mode -------------------------------------------------------------------------

async def test_payg_authorize_allows_when_no_spend_cap_set(db_session):
    service = BillingService(default_mode="payg")

    result = await service.authorize(
        "t1", UsageEstimate(quantities={"cpu_second": 999.0}), session=db_session
    )

    assert result.authorized is True


async def test_payg_authorize_blocks_when_spend_cap_would_be_exceeded(db_session):
    service = BillingService(default_mode="payg")
    await service.set_mode("t1", "payg", spend_cap=10.0, session=db_session)
    await _add_rule(db_session, "cpu_second", 1.0)

    result = await service.authorize(
        "t1", UsageEstimate(quantities={"cpu_second": 20.0}), session=db_session
    )

    assert result.authorized is False
    assert "spend cap" in result.reason


async def test_payg_authorize_accounts_for_usage_already_spent_this_cycle(db_session):
    service = BillingService(default_mode="payg")
    await service.set_mode("t1", "payg", spend_cap=10.0, session=db_session)
    await _add_rule(db_session, "cpu_second", 1.0)
    await service.record_usage(
        "t1", UsageEvent(resource_type="cpu_second", quantity=8.0), session=db_session
    )
    await db_session.commit()

    result = await service.authorize(
        "t1", UsageEstimate(quantities={"cpu_second": 5.0}), session=db_session
    )

    assert result.authorized is False  # 8 already spent + 5 estimated > cap of 10


async def test_payg_record_usage_never_touches_a_wallet(db_session):
    service = BillingService(default_mode="payg")
    await _add_rule(db_session, "cpu_second", 1.0)

    await service.record_usage(
        "t1", UsageEvent(resource_type="cpu_second", quantity=5.0), session=db_session
    )
    await db_session.commit()

    assert await db_session.get(CreditWallet, "t1") is None
    records = (await db_session.execute(select(UsageRecord))).scalars().all()
    assert len(records) == 1


# -- pricing lookup ----------------------------------------------------------------------

async def test_no_pricing_rule_prices_usage_as_free_rather_than_raising(db_session):
    service = BillingService(default_mode="payg")

    await service.record_usage(
        "t1", UsageEvent(resource_type="storage_gb_day", quantity=100.0), session=db_session
    )
    await db_session.commit()

    records = (await db_session.execute(select(UsageRecord))).scalars().all()
    assert records[0].cost == 0.0


async def test_latest_effective_pricing_rule_wins(db_session):
    now = datetime.now(UTC)
    await _add_rule(db_session, "cpu_second", 1.0, effective_from=now - timedelta(days=2))
    await _add_rule(db_session, "cpu_second", 5.0, effective_from=now - timedelta(days=1))
    service = BillingService(default_mode="payg")

    result = await service.authorize(
        "t1", UsageEstimate(quantities={"cpu_second": 2.0}), session=db_session
    )

    assert result.estimated_cost == 10.0  # 2 * 5.0, not 2 * 1.0


async def test_future_effective_pricing_rule_is_ignored_until_it_arrives(db_session):
    now = datetime.now(UTC)
    await _add_rule(db_session, "cpu_second", 1.0, effective_from=now - timedelta(days=1))
    await _add_rule(db_session, "cpu_second", 999.0, effective_from=now + timedelta(days=1))
    service = BillingService(default_mode="payg")

    result = await service.authorize(
        "t1", UsageEstimate(quantities={"cpu_second": 2.0}), session=db_session
    )

    assert result.estimated_cost == 2.0


# -- admin surface: crediting a wallet ---------------------------------------------------

async def test_adjust_credit_tops_up_a_brand_new_wallet(db_session):
    service = BillingService(default_mode="credit")

    wallet = await service.adjust_credit("t1", 100.0, session=db_session)

    assert wallet.tenant_id == "t1"
    assert wallet.balance == 100.0
    ledger = (await db_session.execute(select(CreditLedgerEntry))).scalars().one()
    assert ledger.delta == 100.0
    assert ledger.balance_after == 100.0
    assert ledger.reason == "admin_adjustment"


async def test_adjust_credit_accumulates_across_calls(db_session):
    service = BillingService(default_mode="credit")

    await service.adjust_credit("t1", 100.0, session=db_session)
    wallet = await service.adjust_credit("t1", 50.0, reason="promo", session=db_session)

    assert wallet.balance == 150.0
    entries = (await db_session.execute(select(CreditLedgerEntry).order_by(CreditLedgerEntry.created_at))).scalars().all()
    assert [e.reason for e in entries] == ["admin_adjustment", "promo"]


async def test_adjust_credit_with_negative_delta_deducts(db_session):
    service = BillingService(default_mode="credit")
    await service.adjust_credit("t1", 100.0, session=db_session)

    wallet = await service.adjust_credit("t1", -30.0, reason="correction", session=db_session)

    assert wallet.balance == 70.0


# -- admin surface: mode switching / pricing rules --------------------------------------

async def test_set_mode_creates_account_with_requested_mode_and_spend_cap(db_session):
    service = BillingService(default_mode="credit")

    account = await service.set_mode("t1", "payg", spend_cap=42.0, session=db_session)

    assert account.tenant_id == "t1"
    assert account.mode == "payg"
    assert account.spend_cap == 42.0


async def test_set_mode_rejects_unknown_mode(db_session):
    service = BillingService(default_mode="credit")

    try:
        await service.set_mode("t1", "bogus", spend_cap=None, session=db_session)
        assert False, "expected KubeSandboxError"
    except KubeSandboxError:
        pass


async def test_add_pricing_rule_persists_and_is_retrievable(db_session):
    service = BillingService(default_mode="credit")

    rule = await service.add_pricing_rule("memory_gb_second", 0.05, session=db_session)

    assert rule.id is not None
    fetched = await db_session.get(PricingRule, rule.id)
    assert float(fetched.unit_cost) == 0.05


# -- settle() / invoice drafts -----------------------------------------------------------

async def test_settle_generates_draft_invoice_summing_usage_in_period(db_session):
    now = datetime.now(UTC)
    service = BillingService(default_mode="payg")
    await _add_rule(db_session, "cpu_second", 2.0)
    await service.record_usage("t1", UsageEvent(resource_type="cpu_second", quantity=3.0), session=db_session)
    await service.record_usage("t1", UsageEvent(resource_type="cpu_second", quantity=4.0), session=db_session)
    await db_session.commit()

    invoice = await service.settle(
        "t1", BillingPeriod(start=now - timedelta(hours=1), end=now + timedelta(hours=1)), session=db_session
    )

    assert invoice.status == "draft"
    assert invoice.total_cost == 14.0  # (3 + 4) * 2.0


async def test_settle_excludes_usage_outside_the_period(db_session):
    now = datetime.now(UTC)
    service = BillingService(default_mode="payg")
    await _add_rule(db_session, "cpu_second", 1.0)
    await service.record_usage("t1", UsageEvent(resource_type="cpu_second", quantity=100.0), session=db_session)
    await db_session.commit()

    invoice = await service.settle(
        "t1", BillingPeriod(start=now + timedelta(days=1), end=now + timedelta(days=2)), session=db_session
    )

    assert invoice.total_cost == 0


# -- credit/overusage request workflow ---------------------------------------------------

async def test_request_credit_creates_a_pending_row(db_session):
    service = BillingService(default_mode="credit")

    request = await service.request_credit("t1", 50.0, reason="ran out mid-demo", user_id="u1", session=db_session)

    assert request.status == "pending"
    assert request.tenant_id == "t1"
    assert request.amount == 50.0


async def test_request_credit_rejects_non_positive_amounts(db_session):
    service = BillingService(default_mode="credit")

    try:
        await service.request_credit("t1", 0, reason="nope", user_id=None, session=db_session)
        assert False, "expected KubeSandboxError"
    except KubeSandboxError:
        pass


async def test_list_credit_requests_filters_by_tenant_and_status(db_session):
    service = BillingService(default_mode="credit")
    await service.request_credit("t1", 10.0, reason="a", user_id=None, session=db_session)
    r2 = await service.request_credit("t1", 20.0, reason="b", user_id=None, session=db_session)
    await service.request_credit("t2", 30.0, reason="c", user_id=None, session=db_session)
    await service.review_credit_request(r2.id, approve=False, reviewer="admin-1", note="denied", session=db_session)

    t1_only = await service.list_credit_requests(tenant_id="t1", session=db_session)
    assert {r.tenant_id for r in t1_only} == {"t1"}
    assert len(t1_only) == 2

    pending_only = await service.list_credit_requests(status="pending", session=db_session)
    assert all(r.status == "pending" for r in pending_only)
    assert r2.id not in {r.id for r in pending_only}


async def test_approving_a_credit_request_tops_up_a_credit_mode_wallet(db_session):
    service = BillingService(default_mode="credit")
    request = await service.request_credit("t1", 75.0, reason="need more", user_id=None, session=db_session)

    reviewed = await service.review_credit_request(
        request.id, approve=True, reviewer="admin-1", note="approved", session=db_session
    )

    assert reviewed.status == "approved"
    assert reviewed.reviewed_by == "admin-1"
    wallet = await db_session.get(CreditWallet, "t1")
    assert wallet.balance == 75.0
    ledger = (await db_session.execute(select(CreditLedgerEntry))).scalars().one()
    assert ledger.reason == f"credit_request:{request.id}"


async def test_approving_a_credit_request_raises_spend_cap_for_a_payg_tenant(db_session):
    service = BillingService(default_mode="credit")
    await service.set_mode("t1", "payg", spend_cap=100.0, session=db_session)
    request = await service.request_credit("t1", 50.0, reason="need more headroom", user_id=None, session=db_session)

    await service.review_credit_request(request.id, approve=True, reviewer="admin-1", note=None, session=db_session)

    account = await db_session.get(BillingAccount, "t1")
    assert account.spend_cap == 150.0
    assert await db_session.get(CreditWallet, "t1") is None  # PAYG never gets a wallet


async def test_denying_a_credit_request_applies_nothing(db_session):
    service = BillingService(default_mode="credit")
    request = await service.request_credit("t1", 50.0, reason="nice to have", user_id=None, session=db_session)

    reviewed = await service.review_credit_request(
        request.id, approve=False, reviewer="admin-1", note="not justified", session=db_session
    )

    assert reviewed.status == "denied"
    assert reviewed.review_note == "not justified"
    assert await db_session.get(CreditWallet, "t1") is None


async def test_reviewing_an_already_reviewed_request_raises(db_session):
    service = BillingService(default_mode="credit")
    request = await service.request_credit("t1", 50.0, reason="x", user_id=None, session=db_session)
    await service.review_credit_request(request.id, approve=True, reviewer="admin-1", note=None, session=db_session)

    try:
        await service.review_credit_request(request.id, approve=True, reviewer="admin-1", note=None, session=db_session)
        assert False, "expected KubeSandboxError"
    except KubeSandboxError:
        pass


async def test_reviewing_an_unknown_request_raises(db_session):
    service = BillingService(default_mode="credit")

    try:
        await service.review_credit_request("does-not-exist", approve=True, reviewer="admin-1", note=None, session=db_session)
        assert False, "expected KubeSandboxError"
    except KubeSandboxError:
        pass
