from __future__ import annotations

import pytest

from app.core.errors import KubeSandboxError, QuotaExceededError
from app.services.workspace_service import WorkspaceService
from tests.unit.fakes import FakeObjectStorageProvider, FakeProvisioner


async def test_get_or_create_creates_workspace_with_default_quota(db_session):
    service = WorkspaceService(default_quota_mb=10240)

    workspace = await service.get_or_create("user-1", session=db_session)

    assert workspace.user_id == "user-1"
    assert workspace.quota_mb == 10240
    assert workspace.state == "active"


async def test_get_or_create_is_idempotent_per_user(db_session):
    service = WorkspaceService(default_quota_mb=10240)

    first = await service.get_or_create("user-1", session=db_session)
    second = await service.get_or_create("user-1", session=db_session)

    assert first.id == second.id


async def test_check_quota_raises_when_over(db_session):
    service = WorkspaceService(default_quota_mb=1024)
    workspace = await service.get_or_create("user-1", session=db_session)
    workspace.used_mb = 2000

    with pytest.raises(QuotaExceededError):
        service.check_quota(workspace)


async def test_check_quota_passes_when_within_limit(db_session):
    service = WorkspaceService(default_quota_mb=1024)
    workspace = await service.get_or_create("user-1", session=db_session)
    workspace.used_mb = 500

    service.check_quota(workspace)  # must not raise


async def test_restore_brings_archived_workspace_back_to_active(db_session):
    service = WorkspaceService(default_quota_mb=1024)
    workspace = await service.get_or_create("user-1", session=db_session)
    workspace.state = "archived"
    storage = FakeObjectStorageProvider()
    storage.store[f"workspaces/{workspace.id}/archive.tar.gz"] = b"tar-payload"
    provisioner = FakeProvisioner()

    await service.restore(
        workspace, provisioner=provisioner, object_storage=storage, archiver_image="kubesandbox/base:1.0"
    )

    assert workspace.state == "active"
    assert provisioner.restored_workspaces == [(workspace.id, b"tar-payload")]


async def test_restore_raises_when_workspace_is_not_archived(db_session):
    service = WorkspaceService(default_quota_mb=1024)
    workspace = await service.get_or_create("user-1", session=db_session)  # state == "active"

    with pytest.raises(KubeSandboxError, match="not archived"):
        await service.restore(
            workspace,
            provisioner=FakeProvisioner(),
            object_storage=FakeObjectStorageProvider(),
            archiver_image="kubesandbox/base:1.0",
        )


async def test_touch_updates_last_access(db_session):
    # SQLite's server_default=func.now() (used by get_or_create's insert) returns a
    # naive datetime, unlike touch()'s tz-aware datetime.now(UTC) — a SQLite-only
    # quirk (Postgres' timestamptz is tz-aware either way), so this only checks
    # touch() actually assigns a fresh, tz-aware value, not a before/after ordering.
    service = WorkspaceService(default_quota_mb=1024)
    workspace = await service.get_or_create("user-1", session=db_session)

    service.touch(workspace)

    assert workspace.last_access_at.tzinfo is not None
