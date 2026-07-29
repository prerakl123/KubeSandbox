from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.errors import ProvisionerError
from app.domain.execution import ResourceSpec, SandboxHandle, SandboxSpec, SidecarSpec, WeightClass
from app.persistence.models import PoolMember, PoolState
from app.services.pool_manager import PoolManager
from tests.unit.fakes import FakeProvisioner


def _spec(**overrides) -> SandboxSpec:
    defaults = dict(
        image="kubesandbox/python:3.12.4-slim",
        command=["sleep", "infinity"],
        resources=ResourceSpec(cpu="1", memory="512Mi"),
        weight_class=WeightClass.LIGHT,
    )
    defaults.update(overrides)
    return SandboxSpec(**defaults)


async def test_try_claim_misses_on_empty_pool(db_session):
    manager = PoolManager(FakeProvisioner())
    handle = await manager.try_claim(_spec(), session=db_session)
    assert handle is None


async def test_release_then_claim_round_trips(db_session):
    provisioner = FakeProvisioner()
    manager = PoolManager(provisioner)
    spec = _spec()
    original = SandboxHandle(
        sandbox_id="sb-1", backend="fake", native_ref="container-1", created_at=datetime.now(UTC)
    )

    await manager.release(original, spec, session=db_session)
    await db_session.commit()

    assert provisioner.recycled == ["sb-1"]
    members = (await db_session.execute(select(PoolMember))).scalars().all()
    assert len(members) == 1
    assert members[0].image_ref == spec.image
    assert members[0].native_ref == "container-1"

    claimed = await manager.try_claim(spec, session=db_session)
    await db_session.commit()

    assert claimed is not None
    assert claimed.native_ref == "container-1"
    assert claimed.backend == "fake"
    # Claimed member is gone — a second claim attempt misses.
    assert (await manager.try_claim(spec, session=db_session)) is None


async def test_claim_only_matches_image_and_weight_class(db_session):
    provisioner = FakeProvisioner()
    manager = PoolManager(provisioner)
    handle = SandboxHandle(sandbox_id="sb-1", backend="fake", native_ref="container-1", created_at=datetime.now(UTC))
    await manager.release(handle, _spec(image="python:3.12"), session=db_session)
    await db_session.commit()

    assert await manager.try_claim(_spec(image="node:20"), session=db_session) is None
    assert (
        await manager.try_claim(_spec(image="python:3.12", weight_class=WeightClass.HEAVY), session=db_session)
        is None
    )
    assert await manager.try_claim(_spec(image="python:3.12"), session=db_session) is not None


async def test_release_recycle_failure_destroys_instead_of_pooling(db_session):
    provisioner = FakeProvisioner()
    provisioner.raise_on_recycle = ProvisionerError("container is unhealthy")
    manager = PoolManager(provisioner)
    handle = SandboxHandle(sandbox_id="sb-1", backend="fake", native_ref="container-1", created_at=datetime.now(UTC))

    await manager.release(handle, _spec(), session=db_session)
    await db_session.commit()

    assert provisioner.destroyed == ["sb-1"]
    assert (await db_session.execute(select(PoolMember))).scalars().all() == []


@pytest.mark.parametrize(
    "spec_kwargs",
    [
        {"sidecars": [SidecarSpec(name="db", image="postgres:16", resources=ResourceSpec(cpu="1", memory="256Mi"), uid=999)]},
        {"workspace_id": "ws-1"},
    ],
)
async def test_non_poolable_specs_never_claimed_or_released(db_session, spec_kwargs):
    provisioner = FakeProvisioner()
    manager = PoolManager(provisioner)
    spec = _spec(**spec_kwargs)
    assert PoolManager.is_poolable(spec) is False

    handle = SandboxHandle(sandbox_id="sb-1", backend="fake", native_ref="container-1", created_at=datetime.now(UTC))
    await manager.release(handle, spec, session=db_session)
    await db_session.commit()

    # release() on a non-poolable spec destroys instead of pooling it — defense in
    # depth for a caller (SandboxService already gates this) that forgot to check
    # is_poolable() first; a sidecar/persistent-workspace sandbox must never become
    # claimable by an unrelated tenant.
    assert (await db_session.execute(select(PoolMember))).scalars().all() == []
    assert provisioner.recycled == []
    assert provisioner.destroyed == ["sb-1"]

    assert await manager.try_claim(spec, session=db_session) is None


async def test_replenish_one_tops_up_to_target(db_session):
    provisioner = FakeProvisioner()
    manager = PoolManager(provisioner)
    spec = _spec()

    added = await manager.replenish_one(spec, target_count=3, session=db_session)
    await db_session.commit()

    assert added == 3
    assert len(provisioner.acquired) == 3
    members = (await db_session.execute(select(PoolMember))).scalars().all()
    assert len(members) == 3

    state = (await db_session.execute(select(PoolState))).scalar_one()
    assert state.idle_count == 3


async def test_replenish_one_is_a_noop_when_already_at_target(db_session):
    provisioner = FakeProvisioner()
    manager = PoolManager(provisioner)
    spec = _spec()

    await manager.replenish_one(spec, target_count=2, session=db_session)
    await db_session.commit()
    provisioner.acquired.clear()

    added = await manager.replenish_one(spec, target_count=2, session=db_session)
    await db_session.commit()

    assert added == 0
    assert provisioner.acquired == []
