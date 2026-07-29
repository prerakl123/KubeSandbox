"""SandboxService <-> PoolManager wiring (Phase 7, doc §4.3) — pool.enabled's actual
effect on execute()/create_sandbox(), on top of test_pool_manager.py's own
claim/release/replenish unit tests."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import KubeSandboxError, QuotaExceededError
from app.domain.execution import BatchRunResult, ResourceSpec, SandboxSpec, WeightClass
from app.extensions.loader import load_registry
from app.persistence.models import PoolMember, Sandbox, Workspace
from app.services.pool_manager import PoolManager
from app.services.sandbox_service import SandboxService
from app.services.workspace_service import WorkspaceService
from tests.unit.fakes import FakeProvisioner


def _registry():
    return load_registry()


async def test_execute_releases_clean_ad_hoc_run_to_pool_instead_of_destroying(db_session):
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r1", exit_code=0, stdout="hi\n", stderr="", duration_ms=10)
    )
    service = SandboxService(_registry(), provisioner, pool_manager=PoolManager(provisioner))

    await service.execute(language="python", code="print(1)", tenant_id="t1", user_id=None, session=db_session)

    assert provisioner.destroyed == []  # went back to the pool instead
    assert len(provisioner.recycled) == 1
    sandbox_row = (await db_session.execute(select(Sandbox))).scalars().one()
    assert sandbox_row.state == "terminated"  # this tenant's view of it still ends
    members = (await db_session.execute(select(PoolMember))).scalars().all()
    assert len(members) == 1
    assert members[0].native_ref == sandbox_row.native_ref


async def test_execute_second_call_claims_from_pool_instead_of_acquiring(db_session):
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r1", exit_code=0, stdout="hi\n", stderr="", duration_ms=10)
    )
    service = SandboxService(_registry(), provisioner, pool_manager=PoolManager(provisioner))

    await service.execute(language="python", code="print(1)", tenant_id="t1", user_id=None, session=db_session)
    assert len(provisioner.acquired) == 1

    await service.execute(language="python", code="print(2)", tenant_id="t2", user_id=None, session=db_session)

    # Second run reused the pooled container — no second acquire() call.
    assert len(provisioner.acquired) == 1
    assert provisioner.destroyed == []


async def test_execute_destroys_instead_of_pooling_on_timeout(db_session):
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r1", exit_code=0, stdout="", stderr="", duration_ms=10, timed_out=True)
    )
    service = SandboxService(_registry(), provisioner, pool_manager=PoolManager(provisioner))

    await service.execute(language="python", code="while True: pass", tenant_id="t1", user_id=None, session=db_session)

    assert len(provisioner.destroyed) == 1
    assert (await db_session.execute(select(PoolMember))).scalars().all() == []


async def test_execute_destroys_instead_of_pooling_on_exception(db_session):
    provisioner = FakeProvisioner(raise_on_exec=RuntimeError("boom"))
    service = SandboxService(_registry(), provisioner, pool_manager=PoolManager(provisioner))

    try:
        await service.execute(language="python", code="1/0", tenant_id="t1", user_id=None, session=db_session)
    except RuntimeError:
        pass

    assert len(provisioner.destroyed) == 1
    assert (await db_session.execute(select(PoolMember))).scalars().all() == []


async def test_execute_never_pools_a_template_with_sidecars(db_session):
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r1", exit_code=0, stdout="", stderr="", duration_ms=10)
    )
    service = SandboxService(_registry(), provisioner, pool_manager=PoolManager(provisioner))

    await service.execute(
        language="python",
        code="print(1)",
        template="python-postgres-lab@1.0",
        tenant_id="t1",
        user_id=None,
        session=db_session,
    )

    assert len(provisioner.destroyed) == 1
    assert provisioner.recycled == []
    assert (await db_session.execute(select(PoolMember))).scalars().all() == []


async def test_pooling_disabled_by_default_still_always_destroys(db_session):
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r1", exit_code=0, stdout="", stderr="", duration_ms=10)
    )
    service = SandboxService(_registry(), provisioner)  # pool_manager=None, the default

    await service.execute(language="python", code="print(1)", tenant_id="t1", user_id=None, session=db_session)

    assert len(provisioner.destroyed) == 1
    assert provisioner.recycled == []


async def test_heavy_node_segregation_applied_only_to_heavy_specs(db_session):
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r1", exit_code=0, stdout="", stderr="", duration_ms=10)
    )
    service = SandboxService(
        _registry(),
        provisioner,
        heavy_node_selector={"kubesandbox.io/workload-class": "heavy"},
        heavy_tolerations=[{"key": "kubesandbox.io/heavy", "operator": "Exists"}],
    )

    await service.execute(language="python", code="print(1)", tenant_id="t1", user_id=None, session=db_session)

    assert len(provisioner.acquired) == 1
    acquired_spec = provisioner.acquired[0]
    # python@3.12.4 is "light" weight class — segregation must not touch it.
    assert acquired_spec.node_selector == {}
    assert acquired_spec.tolerations == []


def test_heavy_node_segregation_applied_to_heavy_specs():
    service = SandboxService(
        _registry(),
        FakeProvisioner(),
        heavy_node_selector={"kubesandbox.io/workload-class": "heavy"},
        heavy_tolerations=[{"key": "kubesandbox.io/heavy", "operator": "Exists"}],
    )
    heavy_spec = SandboxSpec(
        image="kubesandbox/heavy-thing:1.0",
        command=["sleep", "infinity"],
        resources=ResourceSpec(cpu="4", memory="4Gi"),
        weight_class=WeightClass.HEAVY,
    )

    segregated = service._apply_heavy_segregation(heavy_spec)

    assert segregated.node_selector == {"kubesandbox.io/workload-class": "heavy"}
    assert segregated.tolerations == [{"key": "kubesandbox.io/heavy", "operator": "Exists"}]


async def test_create_sandbox_persistent_wires_workspace_onto_spec_and_row(db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(
        _registry(), provisioner, workspace_service=WorkspaceService(default_quota_mb=2048)
    )

    row = await service.create_sandbox(
        language="python", persistent=True, tenant_id="t1", user_id="user-1", session=db_session
    )

    assert row.persistent is True
    assert row.workspace_id is not None
    acquired_spec = provisioner.acquired[0]
    assert acquired_spec.workspace_id == row.workspace_id
    assert acquired_spec.workspace_size_mb == 2048

    workspaces = (await db_session.execute(select(Workspace))).scalars().all()
    assert len(workspaces) == 1
    assert workspaces[0].id == row.workspace_id


async def test_create_sandbox_persistent_reuses_same_workspace_across_calls(db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(
        _registry(), provisioner, workspace_service=WorkspaceService(default_quota_mb=2048)
    )

    first = await service.create_sandbox(
        language="python", persistent=True, tenant_id="t1", user_id="user-1", session=db_session
    )
    second = await service.create_sandbox(
        language="node", persistent=True, tenant_id="t1", user_id="user-1", session=db_session
    )

    assert first.workspace_id == second.workspace_id
    assert len((await db_session.execute(select(Workspace))).scalars().all()) == 1


async def test_create_sandbox_persistent_without_workspace_service_raises(db_session):
    service = SandboxService(_registry(), FakeProvisioner())  # workspace_service=None default

    with pytest.raises(KubeSandboxError, match="not enabled"):
        await service.create_sandbox(
            language="python", persistent=True, tenant_id="t1", user_id="user-1", session=db_session
        )


async def test_create_sandbox_persistent_without_user_id_raises(db_session):
    service = SandboxService(
        _registry(), FakeProvisioner(), workspace_service=WorkspaceService(default_quota_mb=2048)
    )

    with pytest.raises(KubeSandboxError, match="authenticated user"):
        await service.create_sandbox(
            language="python", persistent=True, tenant_id="t1", user_id=None, session=db_session
        )


async def test_create_sandbox_persistent_over_quota_raises(db_session):
    workspace_service = WorkspaceService(default_quota_mb=100)
    service = SandboxService(_registry(), FakeProvisioner(), workspace_service=workspace_service)
    workspace = await workspace_service.get_or_create("user-1", session=db_session)
    workspace.used_mb = 500
    await db_session.commit()

    with pytest.raises(QuotaExceededError):
        await service.create_sandbox(
            language="python", persistent=True, tenant_id="t1", user_id="user-1", session=db_session
        )


async def test_create_sandbox_persistent_on_archived_workspace_raises_not_silently_restores(db_session):
    workspace_service = WorkspaceService(default_quota_mb=2048)
    service = SandboxService(_registry(), FakeProvisioner(), workspace_service=workspace_service)
    workspace = await workspace_service.get_or_create("user-1", session=db_session)
    workspace.state = "archived"
    await db_session.commit()

    with pytest.raises(KubeSandboxError, match="not active"):
        await service.create_sandbox(
            language="python", persistent=True, tenant_id="t1", user_id="user-1", session=db_session
        )


async def test_destroy_sandbox_never_releases_to_pool(db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(_registry(), provisioner, pool_manager=PoolManager(provisioner))

    row = await service.create_sandbox(language="python", tenant_id="t1", user_id=None, session=db_session)
    await service.destroy_sandbox(row.id, "t1", session=db_session)

    assert provisioner.destroyed == [row.id]
    assert provisioner.recycled == []
    assert (await db_session.execute(select(PoolMember))).scalars().all() == []
