"""BuildManager orchestration tests (doc §8, Phase 6) — real db_session + Registry,
FakeBuildStrategy/FakeImageRegistryProvider/FakeObjectStorageProvider standing in for
the real I/O boundaries (Docker daemon, registry push, object storage), the same
"swap the I/O boundary, keep everything else real" pattern test_sandbox_service.py
uses for SandboxService/FakeProvisioner.
"""

from __future__ import annotations

from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.errors import BuildNotFoundError, ComponentNotFoundError, EntitlementError
from app.domain.auth import Principal
from app.domain.build import Artifact
from app.domain.manifests import ComponentSource, DockerfileSource
from app.extensions.loader import Registry
from app.persistence.models import Base
from app.services.build_manager import BuildManager
from app.services.entitlement_service import EntitlementService
from tests.unit.factories import make_build_component
from tests.unit.fakes import FakeBuildStrategy, FakeImageRegistryProvider, FakeObjectStorageProvider

ADMIN = Principal(tenant_id="tenant-a", user_id="user-a", role="admin")
TENANT_A = Principal(tenant_id="tenant-a", user_id="user-a", role="service")
TENANT_B = Principal(tenant_id="tenant-b", user_id="user-b", role="service")

_DOCKERFILE_SOURCE = ComponentSource(type="dockerfile", dockerfile=DockerfileSource())


@pytest_asyncio.fixture
async def db():
    """Two separate session checkouts (trigger_build's request-scoped one, run_build's
    own) must see the same data — StaticPool shares one connection, unlike the plain
    conftest.py db_session fixture, which only ever needs one session at a time."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session, factory
    await engine.dispose()


def _build_manager(session, session_factory, **kwargs):
    entitlements = EntitlementService(session)
    registry = kwargs.pop("registry", None) or Registry()
    image_registry = kwargs.pop("image_registry", None) or FakeImageRegistryProvider()
    object_storage = kwargs.pop("object_storage", FakeObjectStorageProvider())
    strategies = kwargs.pop("strategies", None)
    return BuildManager(registry, entitlements, image_registry, object_storage, session_factory, strategies=strategies)


def _registry_with(key: str, component) -> Registry:
    registry = Registry()
    registry.components[key] = component
    registry.component_dirs[key] = Path(".")
    return registry


async def test_trigger_build_creates_pending_row_for_admin(db) -> None:
    session, factory = db
    component = make_build_component("jq", "1.0", source=_DOCKERFILE_SOURCE)
    registry = _registry_with("jq@1.0", component)
    manager = _build_manager(session, factory, registry=registry)

    build_row, is_new = await manager.trigger_build("jq@1.0", ADMIN, session)

    assert is_new is True
    assert build_row.status == "pending"
    assert build_row.component_name == "jq"
    assert build_row.tenant_id is None
    assert build_row.strategy == "dockerfile"


async def test_trigger_build_rejects_non_admin_for_public_component(db) -> None:
    session, factory = db
    component = make_build_component("jq", "1.0", source=_DOCKERFILE_SOURCE)
    registry = _registry_with("jq@1.0", component)
    manager = _build_manager(session, factory, registry=registry)

    try:
        await manager.trigger_build("jq@1.0", TENANT_A, session)
        raise AssertionError("expected EntitlementError")
    except EntitlementError:
        pass


async def test_trigger_build_allows_non_admin_for_own_tenant_private_component(db) -> None:
    session, factory = db
    component = make_build_component("mytool", "1.0", source=_DOCKERFILE_SOURCE)
    key = "tenant/tenant-a/mytool@1.0"
    registry = _registry_with(key, component)
    manager = _build_manager(session, factory, registry=registry)

    build_row, is_new = await manager.trigger_build(key, TENANT_A, session)

    assert is_new is True
    assert build_row.tenant_id == "tenant-a"


async def test_trigger_build_rejects_other_tenants_private_component(db) -> None:
    session, factory = db
    component = make_build_component("mytool", "1.0", source=_DOCKERFILE_SOURCE)
    key = "tenant/tenant-a/mytool@1.0"
    registry = _registry_with(key, component)
    manager = _build_manager(session, factory, registry=registry)

    try:
        await manager.trigger_build(key, TENANT_B, session)
        raise AssertionError("expected EntitlementError")
    except EntitlementError:
        pass


async def test_trigger_build_unknown_component_raises(db) -> None:
    session, factory = db
    manager = _build_manager(session, factory, registry=Registry())

    try:
        await manager.trigger_build("ghost@1.0", ADMIN, session)
        raise AssertionError("expected ComponentNotFoundError")
    except ComponentNotFoundError:
        pass


async def test_trigger_build_deduplicates_in_flight_build(db) -> None:
    session, factory = db
    component = make_build_component("jq", "1.0", source=_DOCKERFILE_SOURCE)
    registry = _registry_with("jq@1.0", component)
    manager = _build_manager(session, factory, registry=registry)

    first, is_new_1 = await manager.trigger_build("jq@1.0", ADMIN, session)
    second, is_new_2 = await manager.trigger_build("jq@1.0", ADMIN, session)

    assert is_new_1 is True
    assert is_new_2 is False
    assert first.id == second.id


async def test_run_build_succeeds_and_populates_built_images(db) -> None:
    session, factory = db
    component = make_build_component("jq", "1.0", source=_DOCKERFILE_SOURCE)
    registry = _registry_with("jq@1.0", component)
    fake_strategy = FakeBuildStrategy(artifact=Artifact(kind="image", ref="kubesandbox/jq:1.0"))
    image_registry = FakeImageRegistryProvider()
    manager = _build_manager(
        session, factory, registry=registry, image_registry=image_registry,
        strategies={"dockerfile": fake_strategy},
    )

    build_row, _ = await manager.trigger_build("jq@1.0", ADMIN, session)
    await manager.run_build(build_row.id)

    # run_build committed via ITS OWN session (a fresh one per request in production,
    # e.g. a FastAPI BackgroundTask) — read back through a fresh session too, mirroring
    # what a real follow-up GET /v1/builds/{id} request (its own new session) would see.
    async with factory() as check_session:
        refreshed = await manager.get_build(build_row.id, ADMIN, check_session)
    assert refreshed.status == "succeeded"
    assert refreshed.image_ref == "registry.local/kubesandbox/jq:1.0"
    assert refreshed.finished_at is not None
    assert fake_strategy.calls == ["jq@1.0"]
    assert image_registry.pushed == ["kubesandbox/jq:1.0"]
    assert registry.built_images["jq@1.0"] == "registry.local/kubesandbox/jq:1.0"


async def test_run_build_manifest_artifact_does_not_populate_built_images(db) -> None:
    session, factory = db
    component = make_build_component("demo-echo", "1.0", source=_DOCKERFILE_SOURCE, category="service")
    registry = _registry_with("demo-echo@1.0", component)
    fake_strategy = FakeBuildStrategy(artifact=Artifact(kind="manifest", ref="helm-artifacts/demo-echo/1.0/manifest.yaml"))
    manager = _build_manager(
        session, factory, registry=registry, strategies={"dockerfile": fake_strategy}
    )

    build_row, _ = await manager.trigger_build("demo-echo@1.0", ADMIN, session)
    await manager.run_build(build_row.id)

    async with factory() as check_session:
        refreshed = await manager.get_build(build_row.id, ADMIN, check_session)
    assert refreshed.status == "succeeded"
    assert refreshed.artifact_ref == "helm-artifacts/demo-echo/1.0/manifest.yaml"
    assert refreshed.image_ref is None
    assert "demo-echo@1.0" not in registry.built_images


async def test_run_build_failure_records_error_and_status_failed(db) -> None:
    session, factory = db
    component = make_build_component("jq", "1.0", source=_DOCKERFILE_SOURCE)
    registry = _registry_with("jq@1.0", component)
    fake_strategy = FakeBuildStrategy(raise_on_build=RuntimeError("dockerfile build failed"))
    manager = _build_manager(
        session, factory, registry=registry, strategies={"dockerfile": fake_strategy}
    )

    build_row, _ = await manager.trigger_build("jq@1.0", ADMIN, session)
    await manager.run_build(build_row.id)

    async with factory() as check_session:
        refreshed = await manager.get_build(build_row.id, ADMIN, check_session)
    assert refreshed.status == "failed"
    assert refreshed.error == "dockerfile build failed"
    assert "jq@1.0" not in registry.built_images


async def test_get_build_hides_other_tenants_build_from_non_admin(db) -> None:
    session, factory = db
    component = make_build_component("mytool", "1.0", source=_DOCKERFILE_SOURCE)
    key = "tenant/tenant-a/mytool@1.0"
    registry = _registry_with(key, component)
    manager = _build_manager(session, factory, registry=registry)

    build_row, _ = await manager.trigger_build(key, TENANT_A, session)

    try:
        await manager.get_build(build_row.id, TENANT_B, session)
        raise AssertionError("expected BuildNotFoundError")
    except BuildNotFoundError:
        pass

    # The owning tenant, and an admin, can both see it.
    assert (await manager.get_build(build_row.id, TENANT_A, session)).id == build_row.id
    assert (await manager.get_build(build_row.id, ADMIN, session)).id == build_row.id


async def test_hydrate_built_images_rehydrates_from_latest_successful_build(db) -> None:
    session, factory = db
    component = make_build_component("jq", "1.0", source=_DOCKERFILE_SOURCE)
    registry = _registry_with("jq@1.0", component)
    fake_strategy = FakeBuildStrategy(artifact=Artifact(kind="image", ref="kubesandbox/jq:1.0"))
    manager = _build_manager(
        session, factory, registry=registry, strategies={"dockerfile": fake_strategy}
    )
    build_row, _ = await manager.trigger_build("jq@1.0", ADMIN, session)
    await manager.run_build(build_row.id)

    # Simulate a restart: fresh Registry, no in-memory built_images at all, and a
    # fresh session (matching app/main.py's lifespan, which opens its own).
    fresh_registry = _registry_with("jq@1.0", component)
    fresh_manager = _build_manager(session, factory, registry=fresh_registry)
    assert "jq@1.0" not in fresh_registry.built_images

    async with factory() as hydrate_session:
        await fresh_manager.hydrate_built_images(hydrate_session)

    assert fresh_registry.built_images["jq@1.0"] == "registry.local/kubesandbox/jq:1.0"
