"""Tests for the real auth dependency in `app/api/deps.py` (Phase 9).

Distinct from `test_ui_api_surface.py`, which overrides `get_current_principal` to focus
on the endpoints: here the dependency itself runs, so the bearer/API-key precedence, the
401s, and the `last_used_at` bookkeeping are actually exercised. Without this, "every
endpoint requires auth" would be an untested assumption in a codebase where every other
test hands itself a principal.

`auth.disabled` is forced off for these, since the local profile turns it on and it
short-circuits the whole path by design.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.api.deps import _bearer_token, _principal_from_api_key, _resolve_principal
from app.core.config import AuthSettings, Settings
from app.domain.auth import Principal
from app.persistence.models import ApiKey, Tenant
from app.services.auth_service import AuthService

_SECRET = "b" * 32
_RAW_KEY = "ks_test_raw_key_value"


@pytest.fixture
def enabled_auth(monkeypatch):
    """Swap in a Settings with auth *on* — the local profile disables it, and the
    disabled path returns the local-dev admin for every request, which would make every
    assertion below vacuous."""
    settings = Settings(auth=AuthSettings(disabled=False, jwt_secret=_SECRET))
    monkeypatch.setattr("app.api.deps.get_settings", lambda: settings)
    return settings


@pytest.fixture
async def key_row(db_session):
    db_session.add(Tenant(id="t1", name="t1"))
    await db_session.flush()
    row = ApiKey(
        id="k1",
        tenant_id="t1",
        key_hash=hashlib.sha256(_RAW_KEY.encode()).hexdigest(),
        label="test",
        prefix=_RAW_KEY[:12],
    )
    db_session.add(row)
    await db_session.commit()
    return row


# -- bearer header parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc.def.ghi", "abc.def.ghi"),
        # RFC 6750 makes the scheme case-insensitive, and real clients differ.
        ("bearer abc", "abc"),
        ("BEARER abc", "abc"),
        ("Bearer   padded  ", "padded"),
        (None, None),
        ("", None),
        ("Basic dXNlcjpwYXNz", None),
        ("Bearer", None),
        ("Bearer    ", None),
        ("abc.def.ghi", None),
    ],
)
def test_bearer_token_extraction(header, expected) -> None:
    assert _bearer_token(header) == expected


# -- credential resolution ------------------------------------------------------------


async def test_no_credentials_is_a_401_naming_both_accepted_forms(db_session, enabled_auth) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await _resolve_principal(db_session, None, None)
    assert excinfo.value.status_code == 401
    # The message names both header options — a bare "missing API key" would send a UI
    # developer looking for the wrong credential entirely.
    assert "Bearer" in excinfo.value.detail and "X-API-Key" in excinfo.value.detail


async def test_a_valid_session_token_resolves_its_principal(db_session, enabled_auth) -> None:
    principal = Principal(tenant_id="t9", user_id="u9", role="admin")
    token = AuthService(enabled_auth.auth).issue_session_token(principal).access_token

    assert await _resolve_principal(db_session, None, token) == principal


async def test_an_invalid_session_token_is_a_401(db_session, enabled_auth) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await _resolve_principal(db_session, None, "not-a-token")
    assert excinfo.value.status_code == 401


async def test_a_valid_api_key_resolves_a_service_principal(db_session, enabled_auth, key_row) -> None:
    principal = await _resolve_principal(db_session, _RAW_KEY, None)
    # A key belongs to a tenant, not a person (doc §11) — so no user, and role `service`,
    # which `require_admin` will never accept.
    assert principal == Principal(tenant_id="t1", user_id=None, role="service")


async def test_an_unknown_api_key_is_a_401(db_session, enabled_auth, key_row) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await _resolve_principal(db_session, "ks_wrong", None)
    assert excinfo.value.status_code == 401


async def test_a_revoked_api_key_is_a_401(db_session, enabled_auth, key_row) -> None:
    """The revoke endpoint only flips a flag; this is the half that makes it mean
    something."""
    from fastapi import HTTPException

    key_row.revoked = True
    await db_session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await _resolve_principal(db_session, _RAW_KEY, None)
    assert excinfo.value.status_code == 401


async def test_bearer_wins_when_both_are_presented(db_session, enabled_auth, key_row) -> None:
    """Documented precedence: bearer is checked first because it's cheaper and it's what
    a UI sends on every request. Asserted so a reordering can't silently downgrade a
    logged-in user to a service principal."""
    principal = Principal(tenant_id="t-bearer", user_id="u", role="user")
    token = AuthService(enabled_auth.auth).issue_session_token(principal).access_token

    assert await _resolve_principal(db_session, _RAW_KEY, token) == principal


async def test_auth_disabled_short_circuits_to_the_local_dev_principal(db_session, monkeypatch) -> None:
    """The local convenience path (refused outside app_env=local by Settings itself)."""
    monkeypatch.setattr("app.api.deps.get_settings", lambda: Settings(auth=AuthSettings(disabled=True)))
    principal = await _resolve_principal(db_session, None, None)
    assert principal.role == "admin"
    assert principal.user_id is not None


# -- last_used_at bookkeeping ---------------------------------------------------------


async def test_api_key_use_stamps_last_used_at(db_session, enabled_auth, key_row) -> None:
    assert key_row.last_used_at is None
    await _principal_from_api_key(db_session, _RAW_KEY)
    await db_session.refresh(key_row)
    assert key_row.last_used_at is not None


async def test_last_used_at_writes_are_coalesced(db_session, enabled_auth, key_row) -> None:
    """A workflow-builder hammering /v1/execute must not turn every request into an
    UPDATE — the stamp is only rewritten once it's more than a minute stale."""
    await _principal_from_api_key(db_session, _RAW_KEY)
    await db_session.refresh(key_row)
    first = key_row.last_used_at

    await _principal_from_api_key(db_session, _RAW_KEY)
    await db_session.refresh(key_row)
    assert key_row.last_used_at == first


async def test_a_stale_last_used_at_is_refreshed(db_session, enabled_auth, key_row) -> None:
    key_row.last_used_at = datetime.now(UTC) - timedelta(hours=2)
    await db_session.commit()
    stale = key_row.last_used_at

    await _principal_from_api_key(db_session, _RAW_KEY)
    await db_session.refresh(key_row)
    assert _as_utc(key_row.last_used_at) > _as_utc(stale)


def _as_utc(value: datetime) -> datetime:
    """SQLite returns a `DateTime(timezone=True)` column as naive (it has no native
    timestamptz); Postgres returns it aware. Normalized here so these assertions hold on
    either — the same asymmetry `deps._is_stale` exists to absorb."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@pytest.mark.parametrize("naive", [True, False])
def test_is_stale_handles_both_naive_and_aware_stamps(naive) -> None:
    """Regression test for a real crash: an unguarded `now - last_used_at` raises
    TypeError on a naive value, and because it sits outside the surrounding
    `suppress(SQLAlchemyError)`, that would have turned a valid API key into a 500."""
    from app.api.deps import _is_stale

    now = datetime.now(UTC)
    recent = now - timedelta(seconds=5)
    old = now - timedelta(hours=2)
    if naive:
        recent = recent.replace(tzinfo=None)
        old = old.replace(tzinfo=None)

    assert _is_stale(None, now) is True
    assert _is_stale(recent, now) is False
    assert _is_stale(old, now) is True


# -- the API surface actually enforces this ------------------------------------------


async def test_every_v1_route_requires_a_credential(monkeypatch) -> None:
    """Sweeps the real OpenAPI spec rather than listing routes by hand, so a new
    endpoint added later can't quietly ship unauthenticated: an untested route added to
    a hand-written list is invisible, but this test sees every path the app declares.

    `/v1/auth/config` is the one deliberate exception — it's what a client calls
    *before* it has a credential.
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    settings = Settings(auth=AuthSettings(disabled=False, jwt_secret=_SECRET))
    monkeypatch.setattr("app.api.deps.get_settings", lambda: settings)

    public = {"/healthz", "/readyz", "/metrics", "/v1/auth/config", "/v1/auth/token"}
    unprotected: list[str] = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path, operations in app.openapi()["paths"].items():
            if path in public:
                continue
            for method in operations:
                # Path params get a value that can't match a real row, so a route that
                # *is* protected answers 401 before ever reaching a lookup.
                url = path.replace("{", "").replace("}", "")
                for placeholder in ("sandbox_id", "run_id", "key_id", "request_id", "tenant_id",
                                    "user_id", "name", "id"):
                    url = url.replace(placeholder, "nonexistent")
                response = await client.request(method.upper(), url, json={})
                if response.status_code != 401:
                    unprotected.append(f"{method.upper()} {path} -> {response.status_code}")

    assert not unprotected, f"routes reachable without credentials: {unprotected}"
