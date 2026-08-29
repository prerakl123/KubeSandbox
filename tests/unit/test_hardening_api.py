"""HTTP-level tests for the post-Phase-9 endpoints and their enforcement.

Separate from `test_hardening.py` (which tests the services directly) because these
assert things only visible through the real routers: that the audit-log and quota
endpoints are admin-gated, that a quota breach surfaces as a 429 rather than a 500, that
`GET /v1/me/quota` is scoped to the caller's own tenant, and that a rate-limit rejection
carries the headers a client needs to back off correctly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    get_current_principal,
    get_entitlement_service,
    get_quota_service,
    get_rate_limiter,
    get_registry,
    get_sandbox_service,
    get_session,
)
from app.core.config import Settings
from app.domain.auth import Principal
from app.domain.execution import BatchRunResult
from app.extensions.loader import Registry
from app.main import app
from app.persistence.models import AuditLog, Quota, Tenant, User
from app.services.audit_service import AuditService
from app.services.quota_service import QuotaService
from app.services.rate_limiter import RateLimiter
from app.services.sandbox_service import SandboxService
from tests.unit.factories import make_component
from tests.unit.fakes import FakeProvisioner
from tests.unit.test_hardening import _FakeRedis

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
ADMIN = Principal(tenant_id=TENANT_A, user_id="user-a", role="admin")
USER = Principal(tenant_id=TENANT_A, user_id="user-a", role="user")


@pytest.fixture
def registry():
    return Registry(
        components={"python@3.12.4": make_component("python", "3.12.4", default_run="echo {file}")},
        templates={},
    )


@pytest.fixture
async def seeded(db_session):
    for tenant_id in (TENANT_A, TENANT_B):
        db_session.add(Tenant(id=tenant_id, name=tenant_id))
    db_session.add(User(id="user-a", tenant_id=TENANT_A, email="a@example.com", role="admin"))
    await db_session.flush()
    db_session.add(AuditLog(id="al-a", tenant_id=TENANT_A, actor="user-a", action="sandbox.run"))
    db_session.add(AuditLog(id="al-b", tenant_id=TENANT_B, actor="user-b", action="sandbox.destroy"))
    await db_session.commit()
    return db_session


@pytest.fixture
async def client(tmp_path, seeded, registry):
    db_session = seeded
    from app.services.entitlement_service import EntitlementService

    entitlements = EntitlementService(db_session)
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r", exit_code=0, stdout="ok\n", stderr="", duration_ms=3)
    )

    async def _override_get_session():
        yield db_session

    @asynccontextmanager
    async def _factory():
        yield db_session

    app.state.audit_service = AuditService(session_factory=lambda: _factory())
    app.state.rate_limiter = RateLimiter(_FakeRedis(), enabled=False)

    app.dependency_overrides[get_current_principal] = lambda: ADMIN
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_entitlement_service] = lambda: entitlements
    app.dependency_overrides[get_sandbox_service] = lambda: SandboxService(
        registry,
        provisioner,
        audit_service=app.state.audit_service,
        session_factory=lambda: _factory(),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    for attr in ("audit_service", "rate_limiter"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def _as(principal: Principal) -> None:
    app.dependency_overrides[get_current_principal] = lambda: principal


# -- audit log endpoint ---------------------------------------------------------------


async def test_audit_log_listing_requires_admin(client) -> None:
    assert (await client.get("/v1/admin/audit-logs")).status_code == 200
    _as(USER)
    assert (await client.get("/v1/admin/audit-logs")).status_code == 403


async def test_audit_log_listing_spans_tenants_for_an_admin(client) -> None:
    """Deliberately not scoped to the caller's tenant: an admin investigating an incident
    needs to see across tenants, and `tenant_id` is there as a filter when they don't."""
    body = (await client.get("/v1/admin/audit-logs")).json()
    assert {i["id"] for i in body["items"]} == {"al-a", "al-b"}


async def test_audit_log_filters(client) -> None:
    by_tenant = (await client.get("/v1/admin/audit-logs", params={"tenant_id": TENANT_B})).json()
    assert [i["id"] for i in by_tenant["items"]] == ["al-b"]

    by_action = (await client.get("/v1/admin/audit-logs", params={"action": "sandbox.run"})).json()
    assert [i["id"] for i in by_action["items"]] == ["al-a"]

    by_actor = (await client.get("/v1/admin/audit-logs", params={"actor": "user-b"})).json()
    assert [i["id"] for i in by_actor["items"]] == ["al-b"]


async def test_audit_log_listing_is_paginated(client) -> None:
    body = (await client.get("/v1/admin/audit-logs", params={"limit": 1})).json()
    assert len(body["items"]) == 1
    assert body["total"] == 2


async def test_a_run_writes_an_audit_entry_with_its_exit_code(client, seeded) -> None:
    """Doc §6 Layer 5 verbatim: "who, what, when, exit code"."""
    await client.post("/v1/execute", json={"language": "python", "code": "print(1)"})

    body = (await client.get("/v1/admin/audit-logs", params={"action": "sandbox.run"})).json()
    entry = next(i for i in body["items"] if i["id"] != "al-a")
    assert entry["detail"]["exit_code"] == 0
    assert entry["detail"]["component"] == "python@3.12.4"
    assert entry["detail"]["run_id"]  # joinable back to the run, not null
    assert entry["actor"] == "user-a"


async def test_audit_detail_never_contains_the_submitted_code(client) -> None:
    """An audit log that accumulates user source becomes both a compliance liability and
    the most attractive table in the database."""
    secret = "SUPER_SECRET_SOURCE_MARKER"
    await client.post("/v1/execute", json={"language": "python", "code": f"x = '{secret}'"})

    body = (await client.get("/v1/admin/audit-logs")).json()
    assert secret not in str(body)


# -- quota endpoints ------------------------------------------------------------------


async def test_quota_endpoints_require_admin(client) -> None:
    assert (await client.get(f"/v1/admin/tenants/{TENANT_A}/quota")).status_code == 200
    _as(USER)
    assert (await client.get(f"/v1/admin/tenants/{TENANT_A}/quota")).status_code == 403
    assert (
        await client.patch(f"/v1/admin/tenants/{TENANT_A}/quota", json={"max_concurrent_sandboxes": 1})
    ).status_code == 403


async def test_get_quota_materializes_defaults_rather_than_404ing(client) -> None:
    body = (await client.get(f"/v1/admin/tenants/{TENANT_B}/quota")).json()
    assert body["tenant_id"] == TENANT_B
    assert body["max_concurrent_sandboxes"] == 10  # the configured default
    assert body["concurrent_sandboxes"] == 0
    # A UI must be told whether the limit it's displaying is actually binding.
    assert body["enabled"] is False


async def test_patch_quota_round_trips(client, seeded) -> None:
    response = await client.patch(
        f"/v1/admin/tenants/{TENANT_A}/quota", json={"max_concurrent_sandboxes": 3}
    )
    assert response.status_code == 200
    assert response.json()["max_concurrent_sandboxes"] == 3
    assert (await seeded.get(Quota, TENANT_A)).max_concurrent_sandboxes == 3


async def test_patch_quota_writes_an_audit_entry(client) -> None:
    await client.patch(f"/v1/admin/tenants/{TENANT_A}/quota", json={"max_monthly_minutes": 50})
    body = (await client.get("/v1/admin/audit-logs", params={"action": "admin.quota_change"})).json()
    assert body["total"] == 1
    assert body["items"][0]["detail"]["max_monthly_minutes"] == 50


async def test_my_quota_is_scoped_to_the_caller(client) -> None:
    _as(USER)
    body = (await client.get("/v1/me/quota")).json()
    assert body["max_concurrent_sandboxes"] == 10
    assert body["concurrent_sandboxes"] == 0
    # Narrower than the admin view on purpose — cpu/memory accounting is an
    # approximation and a precise-looking number a user can't reconcile is worse.
    assert "cpu_millicores" not in body


async def test_quota_breach_is_a_429_not_a_500(client, seeded, registry) -> None:
    """A quota refusal is a client-facing condition (doc §11 groups it with the 429
    family), so it must not surface as a server error."""
    seeded.add(Quota(tenant_id=TENANT_A, max_concurrent_sandboxes=0))
    await seeded.commit()

    quota_service = QuotaService(default_max_concurrent_sandboxes=0)
    app.dependency_overrides[get_quota_service] = lambda: quota_service

    @asynccontextmanager
    async def _factory():
        yield seeded

    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r", exit_code=0, stdout="", stderr="", duration_ms=1)
    )
    app.dependency_overrides[get_sandbox_service] = lambda: SandboxService(
        registry,
        provisioner,
        quota_service=quota_service,
        audit_service=app.state.audit_service,
        session_factory=lambda: _factory(),
    )

    response = await client.post("/v1/execute", json={"language": "python", "code": "print(1)"})

    assert response.status_code == 429
    assert "quota" in response.json()["detail"]
    # Refused before any sandbox was acquired — the whole point of a pre-flight check.
    assert provisioner.acquired == []


async def test_a_quota_denial_is_audited(client, seeded, registry) -> None:
    seeded.add(Quota(tenant_id=TENANT_A, max_concurrent_sandboxes=0))
    await seeded.commit()

    quota_service = QuotaService(default_max_concurrent_sandboxes=0)

    @asynccontextmanager
    async def _factory():
        yield seeded

    app.dependency_overrides[get_sandbox_service] = lambda: SandboxService(
        registry,
        FakeProvisioner(),
        quota_service=quota_service,
        audit_service=app.state.audit_service,
        session_factory=lambda: _factory(),
    )
    await client.post("/v1/execute", json={"language": "python", "code": "print(1)"})

    body = (await client.get("/v1/admin/audit-logs", params={"action": "denied.quota"})).json()
    assert body["total"] == 1


# -- rate limiting --------------------------------------------------------------------


async def test_a_disabled_limiter_does_not_reject(client) -> None:
    for _ in range(5):
        response = await client.post("/v1/execute", json={"language": "python", "code": "print(1)"})
        assert response.status_code == 200


async def test_rate_limited_request_carries_backoff_headers(client, monkeypatch) -> None:
    """A client that can't tell *how long* to wait retries immediately and stays limited;
    a client that only learns its budget by being rejected can't avoid rejection."""
    settings = Settings()
    monkeypatch.setattr("app.api.ratelimit_deps.get_settings", lambda: settings)
    object.__setattr__(settings.rate_limit, "execute_per_minute", 1)

    limiter = RateLimiter(_FakeRedis(), enabled=True)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter

    first = await client.post("/v1/execute", json={"language": "python", "code": "print(1)"})
    assert first.status_code == 200
    assert first.headers["RateLimit-Limit"] == "1"
    # Remaining is present on success too, so a client can back off before being blocked.
    assert first.headers["RateLimit-Remaining"] == "0"

    second = await client.post("/v1/execute", json={"language": "python", "code": "print(1)"})
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1
    assert second.headers["RateLimit-Remaining"] == "0"
    assert second.headers["RateLimit-Policy"] == "1;w=60"


async def test_rate_limit_buckets_are_independent_across_route_classes(client, monkeypatch) -> None:
    """Exhausting the execute budget must not block reads — one shared number would have
    to be set low enough for the expensive path."""
    settings = Settings()
    monkeypatch.setattr("app.api.ratelimit_deps.get_settings", lambda: settings)
    object.__setattr__(settings.rate_limit, "execute_per_minute", 1)

    # One limiter, shared across requests. Constructing it inside the lambda would hand
    # every request a fresh empty store and the limit would never bind.
    limiter = RateLimiter(_FakeRedis(), enabled=True)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter

    await client.post("/v1/execute", json={"language": "python", "code": "print(1)"})
    assert (
        await client.post("/v1/execute", json={"language": "python", "code": "print(1)"})
    ).status_code == 429
    # A read on the same identity is unaffected.
    assert (await client.get("/v1/me")).status_code == 200


async def test_probes_are_never_rate_limited(client, monkeypatch) -> None:
    """The kubelet hits these on a fixed schedule; throttling them would make a busy
    replica look unhealthy and get it killed."""
    settings = Settings()
    monkeypatch.setattr("app.api.ratelimit_deps.get_settings", lambda: settings)
    object.__setattr__(settings.rate_limit, "execute_per_minute", 1)
    limiter = RateLimiter(_FakeRedis(), enabled=True)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter

    for _ in range(5):
        assert (await client.get("/healthz")).status_code == 200


# -- sandbox lifecycle auditing ------------------------------------------------------


async def test_sandbox_create_and_destroy_are_both_audited(client) -> None:
    created = (await client.post("/v1/sandboxes", json={"language": "python"})).json()
    await client.delete(f"/v1/sandboxes/{created['id']}")

    actions = {
        i["action"]
        for i in (await client.get("/v1/admin/audit-logs")).json()["items"]
    }
    assert {"sandbox.create", "sandbox.destroy"} <= actions


async def test_destroy_audit_records_the_lifetime(client) -> None:
    created = (await client.post("/v1/sandboxes", json={"language": "python"})).json()
    await client.delete(f"/v1/sandboxes/{created['id']}")

    # The fixture seeds an `al-b` row with the same action and a null detail, and both
    # share a created_at to the database's resolution — so pick by id rather than trusting
    # the ordering to put ours first.
    body = (await client.get("/v1/admin/audit-logs", params={"action": "sandbox.destroy"})).json()
    entry = next(i for i in body["items"] if i["id"] != "al-b")
    assert entry["detail"]["lifetime_seconds"] is not None
    assert entry["target"] == created["id"]


async def test_api_key_creation_is_audited_without_the_key(client) -> None:
    created = (await client.post("/v1/api-keys", json={"label": "ci"})).json()

    body = (await client.get("/v1/admin/audit-logs", params={"action": "apikey.create"})).json()
    assert body["total"] == 1
    detail = body["items"][0]["detail"]
    assert detail["label"] == "ci"
    assert detail["prefix"] == created["prefix"]
    # The key itself is never recorded anywhere — the prefix is what ties an entry to it.
    assert created["api_key"] not in str(body)


async def test_role_change_audit_records_the_transition(client, seeded) -> None:
    """Recorded as previous -> new so a privilege escalation is visible as a transition
    rather than only as a final state."""
    seeded.add(User(id="user-c", tenant_id=TENANT_A, email="c@example.com", role="user"))
    await seeded.commit()

    await client.patch("/v1/admin/users/user-c/role", json={"role": "admin"})

    body = (await client.get("/v1/admin/audit-logs", params={"action": "admin.role_change"})).json()
    detail = body["items"][0]["detail"]
    assert detail["previous_role"] == "user"
    assert detail["new_role"] == "admin"
