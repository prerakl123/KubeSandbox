"""WorkspaceService.sweep_retention() — doc §10.2's archive/purge state machine
(active -> archived -> deleted), driven by idle days since last_access_at and
absolute age since created_at. See test_workspace_service.py for get_or_create/
check_quota/touch coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.persistence.models import Sandbox, Workspace
from app.services.workspace_service import WorkspaceService
from tests.unit.fakes import FakeObjectStorageProvider, FakeProvisioner


def _make_service() -> WorkspaceService:
    return WorkspaceService(
        default_quota_mb=10240, idle_retention_days=30, archive_grace_days=60, max_lifetime_days=365
    )


async def _add_workspace(db_session, **overrides) -> Workspace:
    defaults = dict(user_id="user-1", quota_mb=10240, state="active")
    defaults.update(overrides)
    ws = Workspace(**defaults)
    db_session.add(ws)
    await db_session.flush()
    return ws


async def test_active_workspace_within_idle_window_is_untouched(db_session):
    now = datetime(2026, 1, 30, tzinfo=UTC)
    ws = await _add_workspace(
        db_session, last_access_at=now - timedelta(days=10), created_at=now - timedelta(days=10)
    )
    await db_session.commit()
    provisioner = FakeProvisioner()
    storage = FakeObjectStorageProvider()

    result = await _make_service().sweep_retention(
        session=db_session, provisioner=provisioner, object_storage=storage, archiver_image="base:1.0", now=now
    )

    assert result.archived == []
    await db_session.refresh(ws)
    assert ws.state == "active"
    assert provisioner.archived_workspaces == []


async def test_idle_active_workspace_is_archived(db_session):
    now = datetime(2026, 1, 30, tzinfo=UTC)
    ws = await _add_workspace(
        db_session, last_access_at=now - timedelta(days=31), created_at=now - timedelta(days=100)
    )
    await db_session.commit()
    provisioner = FakeProvisioner()
    storage = FakeObjectStorageProvider()

    result = await _make_service().sweep_retention(
        session=db_session, provisioner=provisioner, object_storage=storage, archiver_image="base:1.0", now=now
    )

    assert result.archived == [ws.id]
    assert provisioner.archived_workspaces == [ws.id]
    assert provisioner.deleted_workspace_volumes == [ws.id]
    assert f"workspaces/{ws.id}/archive.tar.gz" in storage.store
    await db_session.refresh(ws)
    assert ws.state == "archived"


async def test_workspace_over_max_lifetime_is_archived_even_if_recently_used(db_session):
    now = datetime(2026, 1, 30, tzinfo=UTC)
    ws = await _add_workspace(
        db_session, last_access_at=now - timedelta(days=1), created_at=now - timedelta(days=400)
    )
    await db_session.commit()
    provisioner = FakeProvisioner()

    result = await _make_service().sweep_retention(
        session=db_session,
        provisioner=provisioner,
        object_storage=FakeObjectStorageProvider(),
        archiver_image="base:1.0",
        now=now,
    )

    assert result.archived == [ws.id]


async def test_active_workspace_with_live_sandbox_is_skipped_not_archived(db_session):
    now = datetime(2026, 1, 30, tzinfo=UTC)
    ws = await _add_workspace(
        db_session, last_access_at=now - timedelta(days=31), created_at=now - timedelta(days=100)
    )
    db_session.add(
        Sandbox(
            tenant_id="t1",
            user_id="user-1",
            backend="docker",
            native_ref="c1",
            state="active",
            workspace_id=ws.id,
            persistent=True,
        )
    )
    await db_session.commit()
    provisioner = FakeProvisioner()

    result = await _make_service().sweep_retention(
        session=db_session,
        provisioner=provisioner,
        object_storage=FakeObjectStorageProvider(),
        archiver_image="base:1.0",
        now=now,
    )

    assert result.archived == []
    assert result.skipped_active == [ws.id]
    assert provisioner.archived_workspaces == []
    await db_session.refresh(ws)
    assert ws.state == "active"


async def test_archived_workspace_within_grace_period_is_not_purged(db_session):
    now = datetime(2026, 1, 30, tzinfo=UTC)
    ws = await _add_workspace(
        db_session, state="archived", last_access_at=now - timedelta(days=40), created_at=now - timedelta(days=100)
    )
    await db_session.commit()
    storage = FakeObjectStorageProvider()
    storage.store[f"workspaces/{ws.id}/archive.tar.gz"] = b"data"

    result = await _make_service().sweep_retention(
        session=db_session,
        provisioner=FakeProvisioner(),
        object_storage=storage,
        archiver_image="base:1.0",
        now=now,
    )

    assert result.purged == []
    await db_session.refresh(ws)
    assert ws.state == "archived"


async def test_archived_workspace_past_grace_period_is_purged(db_session):
    now = datetime(2026, 1, 30, tzinfo=UTC)
    ws = await _add_workspace(
        db_session, state="archived", last_access_at=now - timedelta(days=91), created_at=now - timedelta(days=200)
    )
    await db_session.commit()
    storage = FakeObjectStorageProvider()
    archive_key = f"workspaces/{ws.id}/archive.tar.gz"
    storage.store[archive_key] = b"data"

    result = await _make_service().sweep_retention(
        session=db_session,
        provisioner=FakeProvisioner(),
        object_storage=storage,
        archiver_image="base:1.0",
        now=now,
    )

    assert result.purged == [ws.id]
    assert archive_key not in storage.store
    await db_session.refresh(ws)
    assert ws.state == "deleted"


async def test_sweep_refreshes_used_mb_for_idle_active_workspace_with_no_live_sandbox(db_session):
    now = datetime(2026, 1, 30, tzinfo=UTC)
    ws = await _add_workspace(
        db_session, last_access_at=now - timedelta(days=1), created_at=now - timedelta(days=1)
    )
    await db_session.commit()
    provisioner = FakeProvisioner()
    provisioner.usage_by_workspace[ws.id] = 777

    await _make_service().sweep_retention(
        session=db_session,
        provisioner=provisioner,
        object_storage=FakeObjectStorageProvider(),
        archiver_image="base:1.0",
        now=now,
    )

    assert provisioner.measured_workspaces == [ws.id]
    await db_session.refresh(ws)
    assert ws.used_mb == 777


async def test_sweep_skips_usage_measurement_for_workspace_with_live_sandbox(db_session):
    now = datetime(2026, 1, 30, tzinfo=UTC)
    ws = await _add_workspace(
        db_session, last_access_at=now - timedelta(days=1), created_at=now - timedelta(days=1)
    )
    db_session.add(
        Sandbox(
            tenant_id="t1", user_id="user-1", backend="docker", native_ref="c1",
            state="active", workspace_id=ws.id, persistent=True,
        )
    )
    await db_session.commit()
    provisioner = FakeProvisioner()

    await _make_service().sweep_retention(
        session=db_session,
        provisioner=provisioner,
        object_storage=FakeObjectStorageProvider(),
        archiver_image="base:1.0",
        now=now,
    )

    assert provisioner.measured_workspaces == []


async def test_deleted_workspaces_are_never_revisited(db_session):
    now = datetime(2026, 1, 30, tzinfo=UTC)
    await _add_workspace(
        db_session, state="deleted", last_access_at=now - timedelta(days=500), created_at=now - timedelta(days=500)
    )
    await db_session.commit()
    provisioner = FakeProvisioner()

    result = await _make_service().sweep_retention(
        session=db_session,
        provisioner=provisioner,
        object_storage=FakeObjectStorageProvider(),
        archiver_image="base:1.0",
        now=now,
    )

    assert result.archived == []
    assert result.purged == []
    assert provisioner.archived_workspaces == []
