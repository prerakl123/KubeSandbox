"""Exercises HelmChartStrategy + MinIOStorageProvider against a real `helm` binary and
the docker-compose MinIO container (doc §8, Phase 6) — self-skips with a clear reason
if either is unavailable, mirroring test_execute_docker.py's pattern.
"""

from __future__ import annotations

import shutil
import subprocess
import urllib.error
import urllib.request

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.cloud.storage import MinIOStorageProvider
from app.core.config import ObjectStorageSettings
from app.domain.auth import Principal
from app.extensions.loader import load_registry
from app.persistence.models import Base
from app.services.build_manager import BuildManager
from app.services.entitlement_service import EntitlementService

ADMIN = Principal(tenant_id="tenant-1", user_id="user-1", role="admin")

_MINIO_HEALTH_URL = "http://localhost:9000/minio/health/live"


def _helm_available() -> bool:
    return shutil.which("helm") is not None


def _minio_reachable() -> bool:
    try:
        with urllib.request.urlopen(_MINIO_HEALTH_URL, timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not (_helm_available() and _minio_reachable()),
    reason=(
        "requires the `helm` binary on PATH and a reachable MinIO container: "
        "docker compose up -d (see README.md)"
    ),
)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session, factory
    await engine.dispose()


async def test_helm_strategy_renders_and_stores_demo_echo_chart(db) -> None:
    session, session_factory = db

    registry = load_registry()
    entitlements = EntitlementService(session)
    object_storage = MinIOStorageProvider(ObjectStorageSettings())
    manager = BuildManager(registry, entitlements, object_storage=object_storage, image_registry=None, session_factory=session_factory)

    build_row, is_new = await manager.trigger_build("demo-echo@1.0", ADMIN, session)
    assert is_new is True
    await manager.run_build(build_row.id)

    async with session_factory() as check_session:
        finished = await manager.get_build(build_row.id, ADMIN, check_session)
    assert finished.status == "succeeded", finished.error
    assert finished.artifact_ref is not None
    assert finished.image_ref is None
    assert "demo-echo@1.0" not in registry.built_images

    rendered = (await object_storage.get(finished.artifact_ref)).decode()
    assert "kind: Deployment" in rendered
    assert "kind: Service" in rendered
    assert "hello from kubesandbox demo-echo" in rendered
