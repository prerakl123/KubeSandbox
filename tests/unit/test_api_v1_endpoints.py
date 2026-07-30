"""HTTP-level tests for the Phase 2 routers (components/templates/admin) plus the
template-execution path through /v1/execute.

Uses httpx.AsyncClient(transport=ASGITransport(...)) rather than starlette's
TestClient: TestClient runs the ASGI app on a separate thread with its own event loop,
which doesn't mix safely with an asyncio DB session created in the test's own loop
(see tests/integration/test_execute_docker.py's docstring). ASGITransport calls the
app in-process, in the *same* loop as the test — no such conflict.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    get_current_principal,
    get_entitlement_service,
    get_registry_service,
    get_sandbox_service,
    get_template_service,
)
from app.domain.auth import Principal
from app.domain.execution import BatchRunResult
from app.extensions.loader import Registry
from app.main import app
from app.persistence.db import get_session
from app.services.entitlement_service import EntitlementService
from app.services.registry_service import RegistryService
from app.services.sandbox_service import SandboxService
from app.services.template_service import TemplateService
from tests.unit.factories import make_component, make_template
from tests.unit.fakes import FakeProvisioner

ADMIN = Principal(tenant_id="tenant-a", user_id="user-a", role="admin")
TENANT_A = Principal(tenant_id="tenant-a", user_id="user-a", role="service")

_RAW_TOOL_MANIFEST = {
    "apiVersion": "kubesandbox.io/v1",
    "kind": "Component",
    "metadata": {"name": "jq", "version": "1.0", "category": "tool"},
    "spec": {
        "source": {"type": "image", "image": {"repository": "kubesandbox/jq", "tag": "1.0"}},
        "runtime": {
            "kind": "mainTool",
            "resources": {
                "requests": {"cpu": "50m", "memory": "64Mi"},
                "limits": {"cpu": "200m", "memory": "128Mi"},
            },
        },
        "access": {
            "filesystem": {"workdir": "/workspace", "writablePaths": ["/workspace"]},
            "limits": {"processes": 16, "outputBytes": 100000, "wallClockSeconds": 10},
        },
    },
}


@pytest.fixture
def registry():
    return Registry(
        components={
            "python@3.12.4": make_component(
                "python", "3.12.4", default_run="echo {file}"
            )
        },
        templates={},
    )


@pytest.fixture
async def client(tmp_path, db_session, registry):
    entitlements = EntitlementService(db_session)
    registry_service = RegistryService(
        registry, db_session, entitlements, components_dir=tmp_path / "components"
    )
    template_service = TemplateService(
        registry, db_session, entitlements, templates_dir=tmp_path / "templates"
    )
    fake_provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r1", exit_code=0, stdout="ok\n", stderr="", duration_ms=1)
    )
    sandbox_service = SandboxService(registry, fake_provisioner)

    async def _override_get_session():
        yield db_session

    app.dependency_overrides[get_current_principal] = lambda: ADMIN
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_entitlement_service] = lambda: entitlements
    app.dependency_overrides[get_registry_service] = lambda: registry_service
    app.dependency_overrides[get_template_service] = lambda: template_service
    app.dependency_overrides[get_sandbox_service] = lambda: sandbox_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def _as_tenant_a() -> None:
    app.dependency_overrides[get_current_principal] = lambda: TENANT_A


async def test_list_components_entitlement_filtered(client) -> None:
    admin_resp = await client.get("/v1/components")
    assert admin_resp.status_code == 200
    assert [c["key"] for c in admin_resp.json()] == ["python@3.12.4"]

    _as_tenant_a()
    tenant_resp = await client.get("/v1/components")
    assert tenant_resp.status_code == 200
    assert tenant_resp.json() == []


async def test_get_component_versions_includes_schema(client) -> None:
    resp = await client.get("/v1/components/python")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "python"
    assert [v["key"] for v in body["versions"]] == ["python@3.12.4"]
    assert body["json_schema"]["title"] == "KubeSandbox Component manifest"


async def test_get_component_versions_404_when_not_entitled(client) -> None:
    _as_tenant_a()
    resp = await client.get("/v1/components/python")
    assert resp.status_code == 404


async def test_register_component_as_admin(client) -> None:
    resp = await client.post("/v1/components", json=_RAW_TOOL_MANIFEST)
    assert resp.status_code == 201
    assert resp.json()["key"] == "jq@1.0"


async def test_register_component_as_non_admin_without_grant_is_403(client) -> None:
    _as_tenant_a()
    resp = await client.post("/v1/components", json=_RAW_TOOL_MANIFEST)
    assert resp.status_code == 403


async def test_create_and_list_template(client, registry) -> None:
    registry.components["base@1.0"] = make_component("base", "1.0", category="base")
    body = {
        "apiVersion": "kubesandbox.io/v1",
        "kind": "SandboxTemplate",
        "metadata": {"name": "lab", "version": "1.0"},
        "spec": {
            "base": {"ref": "base@1.0"},
            "components": [{"ref": "base@1.0"}],
            "resources": {"cpu": "500m", "memory": "256Mi"},
            "ttl": {"idle": "15m", "max": "2h"},
        },
    }

    create_resp = await client.post("/v1/templates", json=body)
    assert create_resp.status_code == 201
    assert create_resp.json()["key"] == "lab@1.0"

    list_resp = await client.get("/v1/templates")
    assert [t["key"] for t in list_resp.json()] == ["lab@1.0"]


async def test_admin_entitlements_endpoints(client) -> None:
    upsert_resp = await client.patch(
        "/v1/admin/entitlements",
        json={"scope": "tenant", "scope_id": "tenant-a", "component_name": "python"},
    )
    assert upsert_resp.status_code == 200
    assert upsert_resp.json()["component_name"] == "python"

    list_resp = await client.get("/v1/admin/entitlements", params={"scope_id": "tenant-a"})
    assert len(list_resp.json()) == 1


async def test_admin_endpoints_reject_non_admin(client) -> None:
    _as_tenant_a()
    resp = await client.get("/v1/admin/entitlements")
    assert resp.status_code == 403


async def test_admin_publish_grants_endpoints(client) -> None:
    upsert_resp = await client.patch(
        "/v1/admin/publish-grants",
        json={"scope": "tenant", "scope_id": "tenant-a", "category": "tool"},
    )
    assert upsert_resp.status_code == 200
    assert upsert_resp.json()["allowed"] is True

    list_resp = await client.get("/v1/admin/publish-grants", params={"category": "tool"})
    assert len(list_resp.json()) == 1


async def test_admin_billing_endpoints(client) -> None:
    set_resp = await client.patch(
        "/v1/admin/tenants/tenant-a/billing",
        json={"mode": "payg", "spend_cap": 50.0},
    )
    assert set_resp.status_code == 200
    body = set_resp.json()
    assert body == {"tenant_id": "tenant-a", "mode": "payg", "spend_cap": 50.0, "currency": "USD"}

    rule_resp = await client.post(
        "/v1/admin/pricing-rules",
        json={"resource_type": "cpu_second", "unit_cost": 0.0001},
    )
    assert rule_resp.status_code == 200
    rule_body = rule_resp.json()
    assert rule_body["resource_type"] == "cpu_second"
    assert rule_body["unit_cost"] == 0.0001
    assert rule_body["id"]


async def test_admin_billing_endpoints_reject_non_admin(client) -> None:
    _as_tenant_a()
    resp = await client.patch("/v1/admin/tenants/tenant-a/billing", json={"mode": "credit"})
    assert resp.status_code == 403


async def test_admin_credit_adjustment_endpoint(client) -> None:
    top_up = await client.post(
        "/v1/admin/tenants/tenant-a/credit", json={"delta": 100.0, "reason": "initial grant"}
    )
    assert top_up.status_code == 200
    assert top_up.json() == {"tenant_id": "tenant-a", "balance": 100.0}

    deduction = await client.post("/v1/admin/tenants/tenant-a/credit", json={"delta": -25.0})
    assert deduction.status_code == 200
    assert deduction.json()["balance"] == 75.0


async def test_admin_credit_adjustment_endpoint_rejects_non_admin(client) -> None:
    _as_tenant_a()
    resp = await client.post("/v1/admin/tenants/tenant-a/credit", json={"delta": 100.0})
    assert resp.status_code == 403


async def test_credit_request_workflow_end_to_end(client) -> None:
    _as_tenant_a()
    create_resp = await client.post(
        "/v1/billing/credit-requests", json={"amount": 50.0, "reason": "ran out mid-demo"}
    )
    assert create_resp.status_code == 200
    request_id = create_resp.json()["id"]
    assert create_resp.json()["status"] == "pending"

    own_list_resp = await client.get("/v1/billing/credit-requests")
    assert len(own_list_resp.json()) == 1

    # Approving requires an admin — the requester itself can't self-approve.
    approve_as_tenant_resp = await client.patch(
        f"/v1/admin/credit-requests/{request_id}", json={"approve": True}
    )
    assert approve_as_tenant_resp.status_code == 403

    app.dependency_overrides[get_current_principal] = lambda: ADMIN
    admin_list_resp = await client.get("/v1/admin/credit-requests", params={"status": "pending"})
    assert len(admin_list_resp.json()) == 1

    approve_resp = await client.patch(
        f"/v1/admin/credit-requests/{request_id}", json={"approve": True, "note": "approved"}
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "approved"

    # The tenant defaults to credit mode (billing.default_mode) — approving should
    # have topped up its wallet by the requested amount. A zero-delta adjustment is a
    # side-effect-free way to read back the current balance through the existing
    # endpoint rather than adding a GET-wallet route just for this test.
    balance_resp = await client.post("/v1/admin/tenants/tenant-a/credit", json={"delta": 0.0})
    assert balance_resp.json()["balance"] == 50.0


async def test_execute_against_a_template(client, registry) -> None:
    registry.templates["lab@1.0"] = make_template(
        "lab", "1.0", base_ref="python@3.12.4", component_refs=[]
    )
    # "python" component's provides.languageId defaults to None in the shared factory —
    # match on metadata.name instead, so pass language="python".
    resp = await client.post(
        "/v1/execute",
        json={"template": "lab@1.0", "language": "python", "code": "print('hi')"},
    )
    assert resp.status_code == 200
    assert resp.json()["exit_code"] == 0


async def test_execute_rejects_version_alongside_template(client) -> None:
    resp = await client.post(
        "/v1/execute",
        json={"template": "lab@1.0", "language": "python", "version": "3.12.4", "code": "1"},
    )
    assert resp.status_code == 422
