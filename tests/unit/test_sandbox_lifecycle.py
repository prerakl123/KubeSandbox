from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import SandboxNotFoundError
from app.domain.execution import BatchRunResult, SandboxState
from app.extensions.loader import load_registry
from app.persistence.models import Run, Sandbox
from app.services.sandbox_service import SandboxService
from tests.unit.fakes import FakeProvisioner


@pytest.fixture
def registry():
    return load_registry()


async def test_create_sandbox_persists_active_row_and_does_not_destroy(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)

    row = await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id="user-1", session=db_session
    )

    assert row.state == "active"
    assert row.component_refs == ["python@3.12.4"]
    assert provisioner.destroyed == []  # unlike execute(), never torn down here

    rows = (await db_session.execute(select(Sandbox))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == row.id


async def test_get_sandbox_raises_not_found_for_wrong_tenant(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)
    row = await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id=None, session=db_session
    )

    with pytest.raises(SandboxNotFoundError):
        await service.get_sandbox(row.id, "tenant-2", db_session)


async def test_get_sandbox_status_self_heals_when_provisioner_reports_gone(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)
    row = await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id=None, session=db_session
    )

    async def _gone(handle):
        from app.domain.execution import SandboxStatus

        return SandboxStatus(sandbox_id=handle.sandbox_id, state=SandboxState.TERMINATED)

    provisioner.status = _gone  # type: ignore[method-assign]

    refreshed_row, status = await service.get_sandbox_status(row.id, "tenant-1", db_session)
    assert status.state == SandboxState.TERMINATED
    assert refreshed_row.state == "terminated"
    assert refreshed_row.terminated_at is not None


async def test_destroy_sandbox_is_idempotent(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)
    row = await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id=None, session=db_session
    )

    await service.destroy_sandbox(row.id, "tenant-1", db_session)
    assert len(provisioner.destroyed) == 1

    await service.destroy_sandbox(row.id, "tenant-1", db_session)  # must not raise or re-destroy
    assert len(provisioner.destroyed) == 1


async def test_run_in_sandbox_does_not_destroy_and_persists_run(registry, db_session):
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r1", exit_code=0, stdout="hi\n", stderr="", duration_ms=5)
    )
    service = SandboxService(registry, provisioner)
    row = await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id=None, session=db_session
    )

    result = await service.run_in_sandbox(
        row.id, code="print('hi')", tenant_id="tenant-1", session=db_session
    )

    assert result.exit_code == 0
    assert provisioner.destroyed == []
    runs = (await db_session.execute(select(Run))).scalars().all()
    assert len(runs) == 1
    assert runs[0].sandbox_id == row.id


async def test_run_in_terminated_sandbox_raises_not_found(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)
    row = await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id=None, session=db_session
    )
    await service.destroy_sandbox(row.id, "tenant-1", db_session)

    with pytest.raises(SandboxNotFoundError):
        await service.run_in_sandbox(row.id, code="1", tenant_id="tenant-1", session=db_session)
