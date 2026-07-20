"""Exercises BuildManager against a real Docker daemon and the local registry:2
container (doc §8, Phase 6) — the actual risky path a `FakeBuildStrategy`/
`FakeImageRegistryProvider`-backed unit test can't cover. Self-skips with a clear
reason if either isn't reachable, mirroring test_execute_docker.py's pattern exactly.

Bypasses the HTTP/TestClient layer for the same reason test_execute_docker.py does —
calls BuildManager/SandboxService directly.
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

from app.cloud.registry import LocalImageStore
from app.domain.auth import Principal
from app.extensions.loader import load_registry
from app.persistence.models import Base
from app.provisioners.docker import DockerProvisioner
from app.services.build_manager import BuildManager
from app.services.entitlement_service import EntitlementService
from app.services.sandbox_service import SandboxService

_JQ_LOCAL_TAG = "kubesandbox/jq:1.0"
_JQ_REGISTRY_ENDPOINT = "localhost:5000"
_JQ_PUSHED_TAG = f"{_JQ_REGISTRY_ENDPOINT}/{_JQ_LOCAL_TAG}"

ADMIN = Principal(tenant_id="tenant-1", user_id="user-1", role="admin")


def _docker_daemon_reachable() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _local_registry_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"http://{_JQ_REGISTRY_ENDPOINT}/v2/", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not (_docker_daemon_reachable() and _local_registry_reachable()),
    reason=(
        "requires a reachable Docker daemon and the local registry:2 container: "
        "docker compose up -d (see README.md)"
    ),
)


class _FakeImageRegistrySettings:
    endpoint = _JQ_REGISTRY_ENDPOINT


@pytest.fixture
async def provisioner():
    p = DockerProvisioner()
    yield p
    await p.aclose()


@pytest_asyncio.fixture
async def db():
    """Two separate session checkouts (trigger_build's, run_build's own) must see the
    same rows — StaticPool shares one connection, matching test_build_manager.py's
    `db` fixture."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session, factory
    await engine.dispose()


async def test_build_manager_builds_pushes_and_runs_jq(db, provisioner) -> None:
    session, session_factory = db

    # Clean slate: a leftover tag from a previous run would make the "did it really
    # push/pull" assertions below meaningless.
    subprocess.run(["docker", "rmi", "-f", _JQ_LOCAL_TAG, _JQ_PUSHED_TAG], capture_output=True)

    registry = load_registry()
    entitlements = EntitlementService(session)
    image_registry = LocalImageStore(_FakeImageRegistrySettings())
    manager = BuildManager(registry, entitlements, image_registry, None, session_factory)

    build_row, is_new = await manager.trigger_build("jq@1.0", ADMIN, session)
    assert is_new is True
    await manager.run_build(build_row.id)

    async with session_factory() as check_session:
        finished = await manager.get_build(build_row.id, ADMIN, check_session)
    assert finished.status == "succeeded", finished.error
    assert finished.image_ref == _JQ_PUSHED_TAG
    assert registry.built_images["jq@1.0"] == _JQ_PUSHED_TAG

    # Prove the local registry round trip for real: remove the daemon's own copy of
    # the *pushed* tag, so running a sandbox against it can only succeed by actually
    # pulling from localhost:5000 (doc §8.1's ACR-shaped local stand-in), not by
    # reusing an already-cached tag.
    subprocess.run(["docker", "rmi", "-f", _JQ_PUSHED_TAG], capture_output=True, check=True)

    async with session_factory() as run_session:
        service = SandboxService(registry, provisioner)
        result = await service.execute(
            language="jq",
            code='{"hello": "world"}',
            tenant_id="tenant-1",
            user_id=None,
            session=run_session,
        )

    assert result.exit_code == 0
    assert '"hello": "world"' in result.stdout
