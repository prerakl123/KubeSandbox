"""SandboxService <-> BillingService wiring (Phase 8, doc §13) — billing.enabled's
actual effect on execute()/create_sandbox(), on top of test_billing_service.py's own
strategy/pricing unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.errors import BillingAuthorizationError, QuotaExceededError
from app.domain.execution import BatchRunResult
from app.extensions.loader import load_registry
from app.persistence.models import CreditWallet, PricingRule, Run, Sandbox, UsageRecord
from app.services.billing_service import BillingService
from app.services.sandbox_service import SandboxService
from tests.unit.fakes import FakeProvisioner


def _registry():
    return load_registry()


async def _add_rule(session, resource_type: str, unit_cost: float) -> None:
    session.add(PricingRule(resource_type=resource_type, unit_cost=unit_cost))
    await session.flush()


async def _fund_wallet(session, tenant_id: str, balance: float) -> None:
    session.add(CreditWallet(tenant_id=tenant_id, balance=balance))
    await session.flush()


# python's golden image: resources.limits = {cpu: "1", memory: "512Mi"}, wallClockSeconds: 60
# -> authorize() estimate = 1 core * 60s = 60 cpu_second, 0.5 GiB * 60s = 30 memory_gb_second


async def test_execute_blocked_when_credit_balance_is_insufficient(db_session):
    await _add_rule(db_session, "cpu_second", 1.0)
    await _add_rule(db_session, "memory_gb_second", 1.0)
    await _fund_wallet(db_session, "t1", balance=1.0)  # nowhere near the ~90 estimated cost

    provisioner = FakeProvisioner()
    service = SandboxService(_registry(), provisioner, billing_service=BillingService(default_mode="credit"))

    try:
        await service.execute(
            language="python", code="print(1)", tenant_id="t1", user_id=None, session=db_session
        )
        assert False, "expected BillingAuthorizationError"
    except BillingAuthorizationError as exc:
        assert isinstance(exc, QuotaExceededError)  # inherits the 429 mapping

    # Blocked before any resource was provisioned (doc §13).
    assert provisioner.acquired == []
    assert (await db_session.execute(select(Sandbox))).scalars().all() == []


async def test_execute_succeeds_and_records_usage_when_credit_is_sufficient(db_session):
    await _add_rule(db_session, "cpu_second", 1.0)
    await _add_rule(db_session, "memory_gb_second", 1.0)
    await _fund_wallet(db_session, "t1", balance=200.0)

    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r1", exit_code=0, stdout="hi\n", stderr="", duration_ms=2000)
    )
    service = SandboxService(_registry(), provisioner, billing_service=BillingService(default_mode="credit"))

    result = await service.execute(
        language="python", code="print(1)", tenant_id="t1", user_id=None, session=db_session
    )

    assert result.exit_code == 0
    wallet = await db_session.get(CreditWallet, "t1")
    # 2s duration -> 1 core * 2s = 2 cpu_second, 0.5GiB * 2s = 1 memory_gb_second -> cost 3.0
    assert wallet.balance == 197.0

    records = (await db_session.execute(select(UsageRecord))).scalars().all()
    resource_types = {r.resource_type for r in records}
    assert resource_types == {"cpu_second", "memory_gb_second"}
    run_row = (await db_session.execute(select(Run))).scalars().one()
    assert all(r.run_id == run_row.id for r in records)


async def test_execute_without_billing_service_records_no_usage(db_session):
    """billing_service=None (the constructor default, matching billing.enabled: false)
    must reproduce every pre-Phase-8 test's behavior exactly — no wallet, no usage rows,
    no authorization check at all."""
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r1", exit_code=0, stdout="hi\n", stderr="", duration_ms=2000)
    )
    service = SandboxService(_registry(), provisioner)  # billing_service not passed

    result = await service.execute(
        language="python", code="print(1)", tenant_id="t1", user_id=None, session=db_session
    )

    assert result.exit_code == 0
    assert (await db_session.execute(select(UsageRecord))).scalars().all() == []


async def test_create_sandbox_blocked_when_credit_balance_is_insufficient(db_session):
    await _add_rule(db_session, "cpu_second", 1.0)
    await _add_rule(db_session, "memory_gb_second", 1.0)
    await _fund_wallet(db_session, "t1", balance=1.0)

    provisioner = FakeProvisioner()
    service = SandboxService(_registry(), provisioner, billing_service=BillingService(default_mode="credit"))

    try:
        await service.create_sandbox(
            language="python", tenant_id="t1", user_id="user-1", session=db_session
        )
        assert False, "expected BillingAuthorizationError"
    except BillingAuthorizationError:
        pass

    assert provisioner.acquired == []


async def test_create_sandbox_succeeds_but_records_no_usage(db_session):
    """create_sandbox() only ever authorizes (doc §13's "before any resource is
    provisioned") — actual usage for a non-ephemeral sandbox is billed at
    destroy_sandbox() time instead, against its real created_at -> terminated_at span
    (see test_destroy_sandbox_bills_actual_lifetime_usage below), not here."""
    await _add_rule(db_session, "cpu_second", 1.0)
    await _add_rule(db_session, "memory_gb_second", 1.0)
    # create_sandbox() estimates against max_ttl_seconds (default 7200s = 2h), not a
    # single run's wall-clock cap -> 1 core * 7200s + 0.5GiB * 7200s = 10_800 estimated
    # cost at these unit prices; fund comfortably past that.
    await _fund_wallet(db_session, "t1", balance=20_000.0)

    provisioner = FakeProvisioner()
    service = SandboxService(_registry(), provisioner, billing_service=BillingService(default_mode="credit"))

    row = await service.create_sandbox(
        language="python", tenant_id="t1", user_id="user-1", session=db_session
    )

    assert row.state == "active"
    wallet = await db_session.get(CreditWallet, "t1")
    assert wallet.balance == 20_000.0  # authorize() never deducts, only record_usage() does
    assert (await db_session.execute(select(UsageRecord))).scalars().all() == []


async def test_payg_mode_with_no_spend_cap_never_blocks_execute(db_session):
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r1", exit_code=0, stdout="", stderr="", duration_ms=1000)
    )
    service = SandboxService(_registry(), provisioner, billing_service=BillingService(default_mode="payg"))

    result = await service.execute(
        language="python", code="print(1)", tenant_id="t1", user_id=None, session=db_session
    )

    assert result.exit_code == 0
    # PAYG never touches a wallet at all.
    assert await db_session.get(CreditWallet, "t1") is None
    assert len((await db_session.execute(select(UsageRecord))).scalars().all()) == 2


# -- destroy_sandbox() bills a non-ephemeral sandbox's real lifetime usage -------------


async def test_destroy_sandbox_bills_actual_lifetime_usage(db_session):
    await _add_rule(db_session, "cpu_second", 1.0)
    await _add_rule(db_session, "memory_gb_second", 1.0)
    await _fund_wallet(db_session, "t1", balance=1_000_000.0)

    provisioner = FakeProvisioner()
    service = SandboxService(_registry(), provisioner, billing_service=BillingService(default_mode="credit"))

    row = await service.create_sandbox(language="python", tenant_id="t1", user_id="user-1", session=db_session)
    # Backdate created_at so the destroy-time billed span is a known, exact ~100s,
    # rather than depending on real wall-clock time between create and destroy here.
    row.created_at = datetime.now(UTC) - timedelta(seconds=100)
    await db_session.commit()

    await service.destroy_sandbox(row.id, "t1", db_session)

    records = (
        await db_session.execute(select(UsageRecord).where(UsageRecord.sandbox_id == row.id))
    ).scalars().all()
    resource_types = {r.resource_type for r in records}
    assert resource_types == {"cpu_second", "memory_gb_second"}
    # python: 1 core limit -> ~100 cpu_second over a ~100s span.
    cpu_record = next(r for r in records if r.resource_type == "cpu_second")
    assert abs(float(cpu_record.quantity) - 100.0) < 2.0

    wallet = await db_session.get(CreditWallet, "t1")
    assert wallet.balance < 1_000_000.0


async def test_destroy_sandbox_is_idempotent_and_never_double_bills(db_session):
    await _add_rule(db_session, "cpu_second", 1.0)
    await _fund_wallet(db_session, "t1", balance=1_000_000.0)
    provisioner = FakeProvisioner()
    service = SandboxService(_registry(), provisioner, billing_service=BillingService(default_mode="credit"))
    row = await service.create_sandbox(language="python", tenant_id="t1", user_id="user-1", session=db_session)
    # SQLite's server_default=func.now() (used by create_sandbox()'s insert) returns a
    # naive datetime, unlike destroy_sandbox()'s tz-aware datetime.now(UTC) default —
    # a SQLite-only quirk (Postgres' timestamptz is tz-aware either way, same note
    # test_workspace_service.py's test_touch_updates_last_access already makes).
    row.created_at = datetime.now(UTC) - timedelta(seconds=10)
    await db_session.commit()

    await service.destroy_sandbox(row.id, "t1", db_session)
    first_count = len((await db_session.execute(select(UsageRecord))).scalars().all())
    await service.destroy_sandbox(row.id, "t1", db_session)  # already terminated -> no-op
    second_count = len((await db_session.execute(select(UsageRecord))).scalars().all())

    assert first_count > 0
    assert second_count == first_count


async def test_destroy_sandbox_without_billing_service_records_no_usage(db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(_registry(), provisioner)  # billing_service not passed
    row = await service.create_sandbox(language="python", tenant_id="t1", user_id="user-1", session=db_session)

    await service.destroy_sandbox(row.id, "t1", db_session)

    assert (await db_session.execute(select(UsageRecord))).scalars().all() == []
