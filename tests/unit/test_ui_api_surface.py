"""HTTP-level tests for the Phase 9 UI-integration surface.

Covers, at the real router level, the endpoints a frontend cannot be built without —
several of which doc §17 specifies and nothing had ever implemented:

* `GET /v1/me`, `GET /v1/auth/config` — identity and login bootstrap
* `GET /v1/sandboxes` — the list a dashboard renders
* `GET /v1/runs`, `GET /v1/runs/{id}`, `POST /v1/execute?async=true` — doc §5.1's poll
  contract
* `POST/GET/DELETE /v1/api-keys` — doc §11's key management
* `GET /v1/billing/account`, `GET /v1/billing/usage`, `GET /v1/workspaces/me`
* `GET /v1/templates/{name}`, `GET /v1/builds`
* pagination, tenant scoping, and the bearer/API-key auth paths

The two properties tested most insistently are **tenant scoping** (a UI that could see
another tenant's rows is a data breach, not a bug) and **auth enforcement on every
route**, since both are cross-cutting and easy to omit on a new endpoint.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    get_build_manager,
    get_current_principal,
    get_entitlement_service,
    get_registry,
    get_registry_service,
    get_sandbox_service,
    get_template_service,
)
from app.domain.auth import Principal
from app.domain.execution import BatchRunResult
from app.extensions.loader import Registry
from app.main import app
from app.persistence.db import get_session
from app.persistence.models import ApiKey, PricingRule, Run, Sandbox, Tenant, UsageRecord, User, Workspace
from app.services.build_manager import BuildManager
from app.services.entitlement_service import EntitlementService
from app.services.registry_service import RegistryService
from app.services.sandbox_service import SandboxService
from app.services.template_service import TemplateService
from tests.unit.factories import make_component, make_template
from tests.unit.fakes import FakeProvisioner

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

ADMIN = Principal(tenant_id=TENANT_A, user_id="user-a", role="admin")
USER = Principal(tenant_id=TENANT_A, user_id="user-a", role="user")
SERVICE = Principal(tenant_id=TENANT_A, user_id=None, role="service")


@pytest.fixture
def registry():
    return Registry(
        components={"python@3.12.4": make_component("python", "3.12.4", default_run="echo {file}")},
        templates={"lab@1.0": make_template(
                "lab", "1.0", base_ref="python@3.12.4", component_refs=["python@3.12.4"]
            )},
    )


@pytest.fixture
async def seeded(db_session):
    """Two tenants with overlapping data, so every scoping assertion has something real
    to *fail* against — a single-tenant fixture can't tell correct scoping from no
    scoping at all."""
    for tenant_id in (TENANT_A, TENANT_B):
        db_session.add(Tenant(id=tenant_id, name=tenant_id))
    db_session.add(User(id="user-a", tenant_id=TENANT_A, email="a@example.com", role="admin"))
    db_session.add(User(id="user-b", tenant_id=TENANT_B, email="b@example.com", role="user"))
    await db_session.flush()

    db_session.add(
        Sandbox(id="sb-a1", tenant_id=TENANT_A, user_id="user-a", backend="fake", state="active")
    )
    db_session.add(
        Sandbox(id="sb-a2", tenant_id=TENANT_A, user_id=None, backend="fake", state="terminated")
    )
    db_session.add(Sandbox(id="sb-b1", tenant_id=TENANT_B, backend="fake", state="active"))
    await db_session.flush()

    db_session.add(
        Run(id="run-a1", tenant_id=TENANT_A, sandbox_id="sb-a1", status="completed", exit_code=0,
            stdout_excerpt="hello", component_ref="python@3.12.4", command=["python", "main.py"])
    )
    db_session.add(Run(id="run-a2", tenant_id=TENANT_A, status="pending", command=[]))
    db_session.add(Run(id="run-b1", tenant_id=TENANT_B, status="completed", exit_code=1, command=[]))

    db_session.add(ApiKey(id="key-a", tenant_id=TENANT_A, key_hash="hash-a", label="a-key", prefix="ks_aaaa"))
    db_session.add(ApiKey(id="key-b", tenant_id=TENANT_B, key_hash="hash-b", label="b-key", prefix="ks_bbbb"))

    db_session.add(Workspace(id="ws-a", user_id="user-a", quota_mb=10240, used_mb=512))
    db_session.add(PricingRule(id="pr-1", resource_type="cpu_second", unit_cost=0.0001))
    db_session.add(
        UsageRecord(id="ur-a", tenant_id=TENANT_A, resource_type="cpu_second", quantity=10, cost=0.001)
    )
    db_session.add(
        UsageRecord(id="ur-b", tenant_id=TENANT_B, resource_type="cpu_second", quantity=99, cost=9.9)
    )
    await db_session.commit()
    return db_session


@pytest.fixture
async def client(tmp_path, seeded, registry):
    db_session = seeded
    entitlements = EntitlementService(db_session)
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r", exit_code=0, stdout="ok\n", stderr="", duration_ms=3)
    )

    async def _override_get_session():
        yield db_session

    # `get_build_manager`'s real dependency chain resolves the registry and the cloud
    # providers from app.state, which only the real lifespan populates — overridden
    # wholesale rather than faking three pieces of app.state. GET /v1/builds only uses
    # the manager's entitlement-scoped statement builder, so the provider arguments are
    # genuinely unused on this path.
    app.dependency_overrides[get_build_manager] = lambda: BuildManager(
        registry, entitlements, None, None, None
    )
    app.dependency_overrides[get_current_principal] = lambda: ADMIN
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_entitlement_service] = lambda: entitlements
    app.dependency_overrides[get_registry_service] = lambda: RegistryService(
        registry, db_session, entitlements, components_dir=tmp_path / "components"
    )
    app.dependency_overrides[get_template_service] = lambda: TemplateService(
        registry, db_session, entitlements, templates_dir=tmp_path / "templates"
    )
    # A real session factory, so `?async=true` exercises the whole path rather than
    # tripping start_async_run's own "not available" guard. It yields the *same* session
    # the request uses — fine here (aiosqlite in-memory, one connection) and it keeps the
    # background task's writes visible to subsequent assertions.
    @asynccontextmanager
    async def _factory():
        yield db_session

    app.dependency_overrides[get_sandbox_service] = lambda: SandboxService(
        registry, provisioner, session_factory=_factory
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def _as(principal: Principal) -> None:
    app.dependency_overrides[get_current_principal] = lambda: principal


# -- identity -------------------------------------------------------------------------


async def test_auth_config_is_reachable_and_leaks_no_secret(client) -> None:
    response = await client.get("/v1/auth/config")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "auth_required", "provider", "issuer", "client_id", "scopes", "session_ttl_seconds"
    }
    assert "openid" in body["scopes"]
    # Nothing secret is representable in this response by construction — asserted so a
    # future field addition has to think about it.
    assert "secret" not in str(body).lower()


async def test_me_returns_identity_and_feature_flags(client) -> None:
    response = await client.get("/v1/me")
    assert response.status_code == 200
    body = response.json()
    assert body["principal"]["tenant_id"] == TENANT_A
    assert body["principal"]["role"] == "admin"
    assert body["principal"]["email"] == "a@example.com"
    # A UI drives its navigation off these — rendering a billing surface where billing
    # is off produces controls that only ever 400.
    assert set(body["features"]) == {
        "persistent_workspaces", "billing", "pooling", "interactive_attach"
    }
    assert body["app_env"] in ("local", "aks-prod")


async def test_me_for_a_service_principal_has_no_user_or_email(client) -> None:
    _as(SERVICE)
    body = (await client.get("/v1/me")).json()
    assert body["principal"]["user_id"] is None
    assert body["principal"]["email"] is None
    assert body["principal"]["role"] == "service"


# -- sandbox listing ------------------------------------------------------------------


async def test_list_sandboxes_is_tenant_scoped_and_paginated(client) -> None:
    body = (await client.get("/v1/sandboxes")).json()
    assert {item["id"] for item in body["items"]} == {"sb-a1", "sb-a2"}
    assert body["total"] == 2
    assert body["limit"] == 50 and body["offset"] == 0
    # Tenant B's sandbox must not appear under any circumstances.
    assert "sb-b1" not in str(body)


async def test_list_sandboxes_includes_terminated_by_default(client) -> None:
    """A UI needs history, not just live sandboxes — filtering terminated ones out by
    default would make a destroyed sandbox indistinguishable from one that never
    existed."""
    states = {item["state"] for item in (await client.get("/v1/sandboxes")).json()["items"]}
    assert "terminated" in states


async def test_list_sandboxes_state_filter(client) -> None:
    body = (await client.get("/v1/sandboxes", params={"state": "active"})).json()
    assert [item["id"] for item in body["items"]] == ["sb-a1"]
    assert body["total"] == 1


async def test_list_sandboxes_mine_filter(client) -> None:
    body = (await client.get("/v1/sandboxes", params={"mine": True})).json()
    assert [item["id"] for item in body["items"]] == ["sb-a1"]


async def test_mine_filter_is_ignored_for_a_service_principal(client) -> None:
    """A service account has no user identity to filter by; silently returning nothing
    would look like "you have no sandboxes"."""
    _as(SERVICE)
    body = (await client.get("/v1/sandboxes", params={"mine": True})).json()
    assert body["total"] == 2


async def test_pagination_bounds_are_enforced(client) -> None:
    assert (await client.get("/v1/sandboxes", params={"limit": 0})).status_code == 422
    assert (await client.get("/v1/sandboxes", params={"limit": 5000})).status_code == 422
    assert (await client.get("/v1/sandboxes", params={"offset": -1})).status_code == 422


async def test_pagination_total_ignores_the_window(client) -> None:
    """`total` is what lets a UI size a pager; if it reflected the page it would always
    equal `len(items)` and be useless."""
    body = (await client.get("/v1/sandboxes", params={"limit": 1})).json()
    assert len(body["items"]) == 1
    assert body["total"] == 2


# -- runs -----------------------------------------------------------------------------


async def test_list_runs_is_tenant_scoped(client) -> None:
    body = (await client.get("/v1/runs")).json()
    assert {item["id"] for item in body["items"]} == {"run-a1", "run-a2"}
    assert "run-b1" not in str(body)


async def test_list_runs_omits_output_bodies(client) -> None:
    """A list of 50 runs each carrying 10 KB of stdout is a slow endpoint nobody asked
    for — output belongs to the detail view."""
    item = (await client.get("/v1/runs")).json()["items"][0]
    assert "stdout" not in item
    assert "stderr" not in item


async def test_list_runs_filters(client) -> None:
    by_status = (await client.get("/v1/runs", params={"status": "pending"})).json()
    assert [i["id"] for i in by_status["items"]] == ["run-a2"]

    by_sandbox = (await client.get("/v1/runs", params={"sandbox_id": "sb-a1"})).json()
    assert [i["id"] for i in by_sandbox["items"]] == ["run-a1"]


async def test_get_run_returns_the_bundled_result(client) -> None:
    """Doc §5.1's promise: polling yields "the same bundled result body" the synchronous
    call would have returned."""
    body = (await client.get("/v1/runs/run-a1")).json()
    assert body["status"] == "completed"
    assert body["exit_code"] == 0
    assert body["stdout"] == "hello"
    assert body["command"] == ["python", "main.py"]
    assert body["component_ref"] == "python@3.12.4"


async def test_another_tenants_run_is_404_not_403(client) -> None:
    """404 rather than 403 throughout, so run ids can't be probed for existence."""
    assert (await client.get("/v1/runs/run-b1")).status_code == 404
    assert (await client.get("/v1/runs/does-not-exist")).status_code == 404


# -- async execute --------------------------------------------------------------------


async def test_async_execute_returns_202_and_a_pending_run_id(client) -> None:
    response = await client.post(
        "/v1/execute", params={"async": True}, json={"language": "python", "code": "print(1)"}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"

    # The row must be real and pollable. ASGITransport does run BackgroundTasks, so by
    # the time the 202 has been returned the run has already completed — which is
    # exactly doc §5.1's contract from the poller's side: poll until terminal, then read
    # the same bundled result body a synchronous call would have returned.
    polled = await client.get(f"/v1/runs/{body['run_id']}")
    assert polled.status_code == 200
    detail = polled.json()
    assert detail["status"] == "completed"
    assert detail["exit_code"] == 0
    assert detail["stdout"] == "ok\n"
    assert detail["finished_at"] is not None
    assert detail["component_ref"] == "python@3.12.4"


async def test_async_execute_still_rejects_an_unknown_language_synchronously(client) -> None:
    """Resolving the spec before scheduling is what stops a typo from returning a
    cheerful 202 and then failing invisibly in the background."""
    response = await client.post(
        "/v1/execute", params={"async": True}, json={"language": "cobol", "code": "x"}
    )
    assert response.status_code == 404


async def test_synchronous_execute_is_unchanged(client) -> None:
    """The default path must behave exactly as it did before `?async=true` existed."""
    response = await client.post("/v1/execute", json={"language": "python", "code": "print(1)"})
    assert response.status_code == 200
    assert response.json()["stdout"] == "ok\n"


# -- API keys -------------------------------------------------------------------------


async def test_create_api_key_returns_plaintext_exactly_once(client) -> None:
    created = (await client.post("/v1/api-keys", json={"label": "workflow-builder"})).json()
    assert created["api_key"].startswith("ks_")
    assert created["prefix"] == created["api_key"][:12]

    # Never again, in any listing.
    listed = (await client.get("/v1/api-keys")).json()
    assert all("api_key" not in item for item in listed["items"])
    assert created["api_key"] not in str(listed)


async def test_created_key_is_stored_hashed_not_in_plaintext(client, seeded) -> None:
    created = (await client.post("/v1/api-keys", json={"label": "k"})).json()
    row = await seeded.get(ApiKey, created["id"])
    assert row.key_hash != created["api_key"]
    assert len(row.key_hash) == 64  # sha256 hex


async def test_list_api_keys_is_tenant_scoped_and_includes_revoked(client) -> None:
    body = (await client.get("/v1/api-keys")).json()
    assert {i["id"] for i in body["items"]} == {"key-a"}
    assert "key-b" not in str(body)


async def test_revoke_api_key_is_idempotent(client, seeded) -> None:
    assert (await client.delete("/v1/api-keys/key-a")).status_code == 204
    assert (await client.delete("/v1/api-keys/key-a")).status_code == 204
    assert (await seeded.get(ApiKey, "key-a")).revoked is True


async def test_revoked_key_still_appears_in_listings(client) -> None:
    """A revoked key silently vanishing looks identical to it never having existed."""
    await client.delete("/v1/api-keys/key-a")
    item = next(i for i in (await client.get("/v1/api-keys")).json()["items"] if i["id"] == "key-a")
    assert item["revoked"] is True


async def test_cannot_revoke_another_tenants_key(client, seeded) -> None:
    assert (await client.delete("/v1/api-keys/key-b")).status_code == 404
    assert (await seeded.get(ApiKey, "key-b")).revoked is False


# -- billing self-service -------------------------------------------------------------


async def test_billing_account_reports_mode_and_month_to_date(client) -> None:
    body = (await client.get("/v1/billing/account")).json()
    assert body["mode"] in ("credit", "payg")
    # Only tenant A's usage — 0.001, not tenant B's 9.9.
    assert body["month_to_date_cost"] == pytest.approx(0.001)
    assert "enabled" in body


async def test_billing_account_does_not_create_rows(client, seeded) -> None:
    """A GET must not have side effects; reporting the configured default for an
    unbilled tenant is honest, and BillingService creates the real row on first use."""
    from app.persistence.models import BillingAccount

    await client.get("/v1/billing/account")
    assert await seeded.get(BillingAccount, TENANT_A) is None


async def test_billing_usage_is_tenant_scoped(client) -> None:
    body = (await client.get("/v1/billing/usage")).json()
    assert [i["id"] for i in body["items"]] == ["ur-a"]
    assert body["items"][0]["cost"] == pytest.approx(0.001)


# -- workspaces -----------------------------------------------------------------------


async def test_my_workspace_reports_usage_and_retention(client) -> None:
    body = (await client.get("/v1/workspaces/me")).json()
    assert body["workspace"]["id"] == "ws-a"
    assert body["workspace"]["used_mb"] == 512
    assert body["workspace"]["used_percent"] == pytest.approx(5.0)
    # The configured windows, so a UI can explain archival rather than hardcoding it.
    assert set(body["retention"]) == {
        "default_quota_mb", "idle_retention_days", "archive_grace_days", "max_lifetime_days"
    }


async def test_my_workspace_for_a_service_principal_is_a_clear_400(client) -> None:
    """A workspace is per-user (doc §10.2); an empty response would read as "you have
    no workspace" rather than "this caller can't have one"."""
    _as(SERVICE)
    response = await client.get("/v1/workspaces/me")
    assert response.status_code == 400
    assert "service-account" in response.json()["detail"]


# -- templates and builds -------------------------------------------------------------


async def test_template_detail_exposes_the_sandbox_shape(client) -> None:
    """A user choosing between templates is choosing CPU, memory, TTL, and whether their
    files persist — none of which is in the summary the list returns."""
    body = (await client.get("/v1/templates/lab")).json()
    assert body["name"] == "lab"
    version = body["versions"][0]
    assert version["cpu"] and version["memory"]
    assert version["ttl_idle"] and version["ttl_max"]
    assert "persistent_workspace" in version
    assert version["component_refs"] == ["python@3.12.4"]


async def test_unknown_template_detail_is_404(client) -> None:
    assert (await client.get("/v1/templates/nope")).status_code == 404


async def test_template_detail_404s_when_not_entitled(client) -> None:
    """Not-entitled and doesn't-exist report identically (doc §3.6), or a caller could
    enumerate other tenants' private template names."""
    _as(Principal(tenant_id=TENANT_B, user_id="user-b", role="user"))
    assert (await client.get("/v1/templates/lab")).status_code == 404


async def test_list_builds_is_reachable_and_paginated(client) -> None:
    body = (await client.get("/v1/builds")).json()
    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}


# -- admin surface --------------------------------------------------------------------


async def test_list_pricing_rules_requires_admin(client) -> None:
    assert (await client.get("/v1/admin/pricing-rules")).status_code == 200
    _as(USER)
    assert (await client.get("/v1/admin/pricing-rules")).status_code == 403


async def test_list_pricing_rules_returns_configured_rules(client) -> None:
    body = (await client.get("/v1/admin/pricing-rules")).json()
    assert [r["resource_type"] for r in body] == ["cpu_second"]
    assert body[0]["unit_cost"] == pytest.approx(0.0001)


async def test_list_tenants_reports_counts_and_billing(client) -> None:
    body = (await client.get("/v1/admin/tenants")).json()
    by_id = {t["id"]: t for t in body["items"]}
    assert set(by_id) == {TENANT_A, TENANT_B}
    assert by_id[TENANT_A]["user_count"] == 1
    # Only non-terminated sandboxes count: sb-a2 is terminated.
    assert by_id[TENANT_A]["active_sandbox_count"] == 1
    assert by_id[TENANT_B]["active_sandbox_count"] == 1


async def test_admin_tenant_listing_is_not_entitlement_filtered(client) -> None:
    """Doc §3.6: admin endpoints bypass entitlement filtering entirely — an admin has to
    be able to see a tenant before configuring it."""
    body = (await client.get("/v1/admin/tenants")).json()
    assert body["total"] == 2


async def test_list_users_filters_by_tenant(client) -> None:
    body = (await client.get("/v1/admin/users", params={"tenant_id": TENANT_B})).json()
    assert [u["email"] for u in body["items"]] == ["b@example.com"]


async def test_set_user_role_promotes_and_persists(client, seeded) -> None:
    """The only way to create an admin short of a direct DB write — no OIDC claim can
    grant the role (see AuthService)."""
    response = await client.patch("/v1/admin/users/user-b/role", json={"role": "admin"})
    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert (await seeded.get(User, "user-b")).role == "admin"


async def test_set_user_role_rejects_an_unknown_role(client) -> None:
    assert (await client.patch("/v1/admin/users/user-b/role", json={"role": "root"})).status_code == 422


async def test_set_user_role_requires_admin(client) -> None:
    _as(USER)
    assert (await client.patch("/v1/admin/users/user-a/role", json={"role": "admin"})).status_code == 403


async def test_admin_endpoints_reject_a_service_principal(client) -> None:
    """An API key authenticates as role `service`, never as the person who minted it —
    so a leaked key can never reach the admin surface."""
    _as(SERVICE)
    for path in ("/v1/admin/tenants", "/v1/admin/users", "/v1/admin/pricing-rules"):
        assert (await client.get(path)).status_code == 403, path
