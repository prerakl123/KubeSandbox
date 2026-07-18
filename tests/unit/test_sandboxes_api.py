"""HTTP-level tests for app/api/v1/sandboxes.py: the create/get/destroy/runs CRUD
surface (Phase 4 prerequisite) and the file APIs (doc §5.4). Uses httpx.AsyncClient
over ASGITransport, same reasoning as test_api_v1_endpoints.py's docstring (TestClient's
separate thread/event loop doesn't mix safely with an asyncio session from this loop).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_principal, get_sandbox_service
from app.domain.auth import Principal
from app.domain.execution import FileEntry
from app.extensions.loader import Registry
from app.main import app
from app.persistence.db import get_session
from app.services.sandbox_service import SandboxService
from tests.unit.factories import make_component
from tests.unit.fakes import FakeProvisioner

TENANT_A = Principal(tenant_id="tenant-a", user_id="user-a", role="service")
TENANT_B = Principal(tenant_id="tenant-b", user_id="user-b", role="service")


@pytest.fixture
def registry():
    return Registry(
        components={
            "python@3.12.4": make_component("python", "3.12.4", default_run="echo {file}")
        },
        templates={},
    )


@pytest.fixture
def provisioner():
    return FakeProvisioner(
        files={"/workspace/main.py": b"print('hi')\n"},
        tree=[FileEntry(path="main.py", is_dir=False), FileEntry(path="sub", is_dir=True)],
    )


@pytest.fixture
async def client(db_session, registry, provisioner):
    sandbox_service = SandboxService(registry, provisioner)

    async def _override_get_session():
        yield db_session

    app.dependency_overrides[get_current_principal] = lambda: TENANT_A
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_sandbox_service] = lambda: sandbox_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def _as_tenant_b() -> None:
    app.dependency_overrides[get_current_principal] = lambda: TENANT_B


async def test_create_get_destroy_lifecycle(client) -> None:
    create_resp = await client.post("/v1/sandboxes", json={"language": "python"})
    assert create_resp.status_code == 201
    body = create_resp.json()
    assert body["state"] == "active"
    sandbox_id = body["id"]

    get_resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["state"] == "active"

    destroy_resp = await client.delete(f"/v1/sandboxes/{sandbox_id}")
    assert destroy_resp.status_code == 204

    get_after_resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
    assert get_after_resp.json()["state"] == "terminated"


async def test_get_sandbox_404_for_other_tenant(client) -> None:
    create_resp = await client.post("/v1/sandboxes", json={"language": "python"})
    sandbox_id = create_resp.json()["id"]

    _as_tenant_b()
    resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
    assert resp.status_code == 404


async def test_run_in_sandbox_does_not_destroy_it(client, provisioner) -> None:
    create_resp = await client.post("/v1/sandboxes", json={"language": "python"})
    sandbox_id = create_resp.json()["id"]

    run_resp = await client.post(f"/v1/sandboxes/{sandbox_id}/runs", json={"code": "print('hi')"})
    assert run_resp.status_code == 200
    assert run_resp.json()["exit_code"] == 0

    get_resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
    assert get_resp.json()["state"] == "active"
    assert provisioner.destroyed == []


async def test_download_file(client) -> None:
    create_resp = await client.post("/v1/sandboxes", json={"language": "python"})
    sandbox_id = create_resp.json()["id"]

    resp = await client.get(f"/v1/sandboxes/{sandbox_id}/files", params={"path": "main.py"})
    assert resp.status_code == 200
    assert resp.content == b"print('hi')\n"


async def test_download_file_rejects_path_escape(client) -> None:
    create_resp = await client.post("/v1/sandboxes", json={"language": "python"})
    sandbox_id = create_resp.json()["id"]

    resp = await client.get(f"/v1/sandboxes/{sandbox_id}/files", params={"path": "../../etc/passwd"})
    assert resp.status_code == 400


async def test_upload_file(client, provisioner) -> None:
    create_resp = await client.post("/v1/sandboxes", json={"language": "python"})
    sandbox_id = create_resp.json()["id"]

    resp = await client.put(
        f"/v1/sandboxes/{sandbox_id}/files",
        params={"path": "notes.txt"},
        content=b"hello workspace",
    )
    assert resp.status_code == 204
    assert provisioner.put_files_calls == [{"notes.txt": "hello workspace"}]


async def test_get_tree(client) -> None:
    create_resp = await client.post("/v1/sandboxes", json={"language": "python"})
    sandbox_id = create_resp.json()["id"]

    resp = await client.get(f"/v1/sandboxes/{sandbox_id}/tree")
    assert resp.status_code == 200
    paths = {e["path"] for e in resp.json()}
    assert paths == {"main.py", "sub"}
