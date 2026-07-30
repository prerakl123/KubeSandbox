"""app/reconciler/loop.py — TTL reaping, pool replenishment, workspace retention
sweep, and orphan GC (doc §4.1, §4.3, §10.2, Phase 7). Each job is tested in
isolation, then run_tick() end-to-end with a FakeProvisioner + real Registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import BillingSettings, PoolSettings, ProvisionerSettings, Settings, WorkspaceSettings
from app.domain.execution import NativeSandboxRef
from app.extensions.loader import load_registry
from app.persistence.models import PoolMember, PricingRule, Sandbox, Tenant, User, UsageRecord, Workspace
from app.reconciler.loop import bill_workspace_storage, reap_expired_sandboxes, reap_orphans, replenish_pools, run_tick
from app.services.billing_service import BillingService
from app.services.pool_manager import PoolManager
from app.services.sandbox_service import SandboxService
from tests.unit.fakes import FakeObjectStorageProvider, FakeProvisioner


def registry():
    return load_registry()


def _sandbox(**overrides) -> Sandbox:
    defaults = dict(
        tenant_id="t1",
        user_id="u1",
        backend="fake",
        native_ref="c1",
        state="active",
        idle_ttl_seconds=900,
        max_ttl_seconds=7200,
    )
    defaults.update(overrides)
    return Sandbox(**defaults)


# -- reap_expired_sandboxes -----------------------------------------------------------


async def test_reap_expired_sandboxes_leaves_fresh_sandboxes_alone(db_session):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = _sandbox(last_active_at=now - timedelta(seconds=10), created_at=now - timedelta(seconds=10))
    db_session.add(row)
    await db_session.commit()
    provisioner = FakeProvisioner()
    service = SandboxService(registry(), provisioner)

    reaped = await reap_expired_sandboxes(session=db_session, sandbox_service=service, now=now)

    assert reaped == []
    assert provisioner.destroyed == []


async def test_reap_expired_sandboxes_destroys_past_idle_ttl(db_session):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = _sandbox(
        last_active_at=now - timedelta(seconds=1000),
        created_at=now - timedelta(seconds=1000),
        idle_ttl_seconds=900,
        max_ttl_seconds=None,
    )
    db_session.add(row)
    await db_session.commit()
    provisioner = FakeProvisioner()
    service = SandboxService(registry(), provisioner)

    reaped = await reap_expired_sandboxes(session=db_session, sandbox_service=service, now=now)

    assert reaped == [row.id]
    assert provisioner.destroyed == [row.id]
    await db_session.refresh(row)
    assert row.state == "terminated"


async def test_reap_expired_sandboxes_destroys_past_max_ttl_even_if_recently_active(db_session):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = _sandbox(
        last_active_at=now - timedelta(seconds=5),
        created_at=now - timedelta(seconds=10_000),
        idle_ttl_seconds=None,
        max_ttl_seconds=7200,
    )
    db_session.add(row)
    await db_session.commit()
    provisioner = FakeProvisioner()
    service = SandboxService(registry(), provisioner)

    reaped = await reap_expired_sandboxes(session=db_session, sandbox_service=service, now=now)

    assert reaped == [row.id]


async def test_reap_expired_sandboxes_ignores_already_terminated_rows(db_session):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = _sandbox(
        state="terminated",
        last_active_at=now - timedelta(seconds=100_000),
        created_at=now - timedelta(seconds=100_000),
    )
    db_session.add(row)
    await db_session.commit()
    provisioner = FakeProvisioner()
    service = SandboxService(registry(), provisioner)

    reaped = await reap_expired_sandboxes(session=db_session, sandbox_service=service, now=now)

    assert reaped == []


async def test_reap_expired_sandboxes_ignores_rows_with_no_ttl_set(db_session):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = _sandbox(
        last_active_at=now - timedelta(seconds=1_000_000),
        created_at=now - timedelta(seconds=1_000_000),
        idle_ttl_seconds=None,
        max_ttl_seconds=None,
    )
    db_session.add(row)
    await db_session.commit()
    provisioner = FakeProvisioner()
    service = SandboxService(registry(), provisioner)

    reaped = await reap_expired_sandboxes(session=db_session, sandbox_service=service, now=now)

    assert reaped == []


async def test_reap_expired_sandboxes_bills_real_lifetime_usage_when_billing_enabled(db_session):
    """A TTL reap routes through SandboxService.destroy_sandbox() (see
    app/reconciler/loop.py's module docstring) — this is what makes a warm/interactive
    sandbox's real usage get billed even when nothing ever calls DELETE explicitly
    (doc §13, Phase 8's own "known scope boundary" this closes)."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    created_at = now - timedelta(seconds=1_000)
    row = _sandbox(
        component_refs=["python@3.12.4"],
        created_at=created_at,
        last_active_at=created_at,
        idle_ttl_seconds=900,
        max_ttl_seconds=None,
    )
    db_session.add(row)
    db_session.add(PricingRule(resource_type="cpu_second", unit_cost=1.0))
    db_session.add(PricingRule(resource_type="memory_gb_second", unit_cost=1.0))
    await db_session.commit()

    provisioner = FakeProvisioner()
    service = SandboxService(registry(), provisioner, billing_service=BillingService(default_mode="payg"))

    reaped = await reap_expired_sandboxes(session=db_session, sandbox_service=service, now=now)

    assert reaped == [row.id]
    records = (
        await db_session.execute(select(UsageRecord).where(UsageRecord.sandbox_id == row.id))
    ).scalars().all()
    resource_types = {r.resource_type for r in records}
    assert resource_types == {"cpu_second", "memory_gb_second"}
    # python: 1 core limit, 0.5GiB limit; ~1000s idle-since-created span -> ~1000 cpu_second
    cpu_record = next(r for r in records if r.resource_type == "cpu_second")
    assert abs(float(cpu_record.quantity) - 1000.0) < 5.0


# -- replenish_pools --------------------------------------------------------------


async def test_replenish_pools_tops_up_each_poolable_component(db_session):
    settings = Settings(pool=PoolSettings(enabled=True, light_pool_size=2, standard_pool_size=0, heavy_pool_size=0))
    provisioner = FakeProvisioner()
    pool_manager = PoolManager(provisioner)

    added = await replenish_pools(session=db_session, pool_manager=pool_manager, registry=registry(), settings=settings)
    await db_session.commit()

    # python/node/go/bash are all "light" mainTool language components today —
    # every one of them should get topped up to 2.
    assert added  # at least one component was topped up
    assert all(count == 2 for count in added.values())
    members = (await db_session.execute(select(PoolMember))).scalars().all()
    assert len(members) == sum(added.values())


async def test_replenish_pools_is_noop_when_all_targets_are_zero(db_session):
    settings = Settings(pool=PoolSettings(enabled=True, light_pool_size=0, standard_pool_size=0, heavy_pool_size=0))
    provisioner = FakeProvisioner()
    pool_manager = PoolManager(provisioner)

    added = await replenish_pools(session=db_session, pool_manager=pool_manager, registry=registry(), settings=settings)

    assert added == {}
    assert provisioner.acquired == []


# -- reap_orphans -------------------------------------------------------------------


async def test_reap_orphans_ignores_refs_matching_a_live_sandbox(db_session):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = _sandbox(id="sb-live")
    db_session.add(row)
    await db_session.commit()

    provisioner = FakeProvisioner()
    provisioner.native_sandbox_refs = [
        NativeSandboxRef(sandbox_id="sb-live", native_ref="c1", created_at=now - timedelta(hours=1))
    ]

    reaped = await reap_orphans(session=db_session, provisioner=provisioner, backend="docker", grace_seconds=120, now=now)

    assert reaped == []
    assert provisioner.destroyed == []


async def test_reap_orphans_destroys_unreferenced_resources_past_grace_period(db_session):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    provisioner = FakeProvisioner()
    provisioner.native_sandbox_refs = [
        NativeSandboxRef(sandbox_id="sb-orphan", native_ref="c-orphan", created_at=now - timedelta(hours=1))
    ]

    reaped = await reap_orphans(session=db_session, provisioner=provisioner, backend="docker", grace_seconds=120, now=now)

    assert reaped == ["sb-orphan"]
    assert provisioner.destroyed == ["sb-orphan"]


async def test_reap_orphans_gives_recently_created_resources_a_grace_period(db_session):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    provisioner = FakeProvisioner()
    provisioner.native_sandbox_refs = [
        NativeSandboxRef(sandbox_id="sb-brand-new", native_ref="c-new", created_at=now - timedelta(seconds=5))
    ]

    reaped = await reap_orphans(session=db_session, provisioner=provisioner, backend="docker", grace_seconds=120, now=now)

    assert reaped == []
    assert provisioner.destroyed == []


# -- run_tick (end-to-end) -----------------------------------------------------------


async def test_run_tick_with_everything_disabled_only_reaps_ttl_and_orphans(db_session):
    settings = Settings(
        pool=PoolSettings(enabled=False),
        workspace=WorkspaceSettings(persistence_enabled=False),
        provisioner=ProvisionerSettings(backend="docker"),
    )
    provisioner = FakeProvisioner()

    result = await run_tick(
        session=db_session,
        provisioner=provisioner,
        registry=registry(),
        object_storage=FakeObjectStorageProvider(),
        settings=settings,
    )

    assert result.pool_replenished == {}
    assert result.workspaces_archived == []
    assert result.workspaces_purged == []


async def test_run_tick_archives_idle_workspace_when_persistence_enabled(db_session):
    now_ish = datetime.now(UTC)
    ws = Workspace(
        user_id="user-1",
        quota_mb=10240,
        last_access_at=now_ish - timedelta(days=40),
        created_at=now_ish - timedelta(days=40),
    )
    db_session.add(ws)
    await db_session.commit()

    settings = Settings(
        pool=PoolSettings(enabled=False),
        workspace=WorkspaceSettings(persistence_enabled=True, idle_retention_days=30),
        provisioner=ProvisionerSettings(backend="docker"),
    )
    provisioner = FakeProvisioner()

    result = await run_tick(
        session=db_session,
        provisioner=provisioner,
        registry=registry(),
        object_storage=FakeObjectStorageProvider(),
        settings=settings,
    )

    assert result.workspaces_archived == [ws.id]
    assert provisioner.archived_workspaces == [ws.id]


# -- bill_workspace_storage ---------------------------------------------------------


async def _tenant_user(session, *, tenant_name: str = "t1") -> str:
    tenant = Tenant(name=tenant_name)
    session.add(tenant)
    await session.flush()
    user = User(tenant_id=tenant.id, email=f"{tenant_name}@example.com")
    session.add(user)
    await session.flush()
    return user.id


async def test_bill_workspace_storage_prices_used_mb_against_the_tick_interval(db_session):
    user_id = await _tenant_user(db_session)
    ws = Workspace(user_id=user_id, quota_mb=10240, used_mb=1024)  # 1 GiB
    db_session.add(ws)
    db_session.add(PricingRule(resource_type="storage_gb_day", unit_cost=1.0))
    await db_session.commit()
    billing = BillingService(default_mode="payg")

    billed = await bill_workspace_storage(session=db_session, billing_service=billing, interval_seconds=86_400)

    assert billed == [ws.id]
    record = (await db_session.execute(select(UsageRecord))).scalars().one()
    assert record.resource_type == "storage_gb_day"
    assert abs(float(record.quantity) - 1.0) < 0.001  # 1 GiB * (86400s / 86400s-per-day) = 1 GB-day
    assert float(record.cost) == pytest.approx(1.0, abs=0.001)


async def test_bill_workspace_storage_skips_empty_and_non_active_workspaces(db_session):
    user_id = await _tenant_user(db_session)
    db_session.add(Workspace(user_id=user_id, quota_mb=10240, used_mb=0))  # never measured
    db_session.add(Workspace(user_id=user_id, quota_mb=10240, used_mb=500, state="archived"))
    await db_session.commit()
    billing = BillingService(default_mode="payg")

    billed = await bill_workspace_storage(session=db_session, billing_service=billing, interval_seconds=86_400)

    assert billed == []
    assert (await db_session.execute(select(UsageRecord))).scalars().all() == []


async def test_run_tick_bills_workspace_storage_only_when_billing_enabled(db_session):
    user_id = await _tenant_user(db_session)
    ws = Workspace(user_id=user_id, quota_mb=10240, used_mb=2048)
    db_session.add(ws)
    db_session.add(PricingRule(resource_type="storage_gb_day", unit_cost=0.1))
    await db_session.commit()

    settings = Settings(
        pool=PoolSettings(enabled=False),
        workspace=WorkspaceSettings(persistence_enabled=False),
        provisioner=ProvisionerSettings(backend="docker"),
        billing=BillingSettings(enabled=True, default_mode="payg"),
    )

    result = await run_tick(
        session=db_session,
        provisioner=FakeProvisioner(),
        registry=registry(),
        object_storage=FakeObjectStorageProvider(),
        settings=settings,
    )

    assert result.workspace_storage_billed == [ws.id]


async def test_run_tick_skips_workspace_storage_billing_when_disabled(db_session):
    user_id = await _tenant_user(db_session)
    db_session.add(Workspace(user_id=user_id, quota_mb=10240, used_mb=2048))
    await db_session.commit()

    settings = Settings(
        pool=PoolSettings(enabled=False),
        workspace=WorkspaceSettings(persistence_enabled=False),
        provisioner=ProvisionerSettings(backend="docker"),
        billing=BillingSettings(enabled=False),
    )

    result = await run_tick(
        session=db_session,
        provisioner=FakeProvisioner(),
        registry=registry(),
        object_storage=FakeObjectStorageProvider(),
        settings=settings,
    )

    assert result.workspace_storage_billed == []
