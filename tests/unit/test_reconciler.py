"""app/reconciler/loop.py — TTL reaping, pool replenishment, workspace retention
sweep, and orphan GC (doc §4.1, §4.3, §10.2, Phase 7). Each job is tested in
isolation, then run_tick() end-to-end with a FakeProvisioner + real Registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import PoolSettings, ProvisionerSettings, Settings, WorkspaceSettings
from app.domain.execution import NativeSandboxRef
from app.extensions.loader import load_registry
from app.persistence.models import PoolMember, Sandbox, Workspace
from app.reconciler.loop import reap_expired_sandboxes, reap_orphans, replenish_pools, run_tick
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
