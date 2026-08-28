"""Tests for `app/services/auth_service.py` (doc §11, Phase 9 — the last open Phase 0 item).

Weighted heavily toward the *negative* cases, because this is the one module where a
permissive bug is a full authentication bypass rather than a wrong answer. In particular
the algorithm-pinning tests: without `algorithms=[...]`, PyJWT honors the token's own
`alg` header, and both `alg: none` and an RS256 token verified against our HMAC secret
are real, published attacks.

OIDC token *verification* is not tested against a live IdP — that needs a real JWKS
endpoint and an issuer-signed token. What's covered here is everything on our side of
that boundary: the claim-to-identity mapping, first-login provisioning, the role rules,
and session token issue/verify end to end.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import jwt
import pytest
from sqlalchemy import select

from app.core.config import AuthSettings
from app.core.errors import ConfigurationError
from app.domain.auth import Principal
from app.persistence.models import Tenant, User
from app.services.auth_service import AuthenticationFailed, AuthService

_SECRET = "a" * 32  # 32 bytes: RFC 7518 §3.2's floor for HS256


def _service(**overrides) -> AuthService:
    return AuthService(AuthSettings(jwt_secret=_SECRET, **overrides))


def _session_payload(**overrides) -> dict:
    now = int(time.time())
    payload = {
        "iss": "kubesandbox",
        "aud": "kubesandbox-api",
        "sub": "user-1",
        "tid": "tenant-1",
        "role": "user",
        "iat": now,
        "exp": now + 60,
    }
    payload.update(overrides)
    return payload


# -- session tokens: the happy path ---------------------------------------------------


def test_issue_then_verify_round_trips_the_principal() -> None:
    service = _service()
    principal = Principal(tenant_id="t1", user_id="u1", role="admin")

    token = service.issue_session_token(principal)

    assert token.expires_in > 0
    assert service.verify_session_token(token.access_token) == principal


def test_session_ttl_is_honored() -> None:
    token = _service(session_ttl_seconds=120).issue_session_token(
        Principal(tenant_id="t1", user_id="u1", role="user")
    )
    # Compared against a window rather than an exact value: issuing takes nonzero time.
    assert 110 <= token.expires_in <= 120
    assert token.expires_at > datetime.now(UTC)


def test_a_service_principal_with_no_user_id_round_trips_as_none() -> None:
    """An API-key caller has no user (doc §11), and `sub` can't be null in a JWT — it's
    stored as "" and must come back as `None`, not as an empty-string user id that would
    then be used as a foreign key."""
    service = _service()
    token = service.issue_session_token(Principal(tenant_id="t1", user_id=None, role="service"))
    assert service.verify_session_token(token.access_token).user_id is None


# -- session tokens: everything that must be rejected ---------------------------------


def test_a_token_signed_with_another_secret_is_rejected() -> None:
    forged = jwt.encode(_session_payload(), "b" * 32, algorithm="HS256")
    with pytest.raises(AuthenticationFailed):
        _service().verify_session_token(forged)


def test_an_unsigned_alg_none_token_is_rejected() -> None:
    """Algorithm confusion: without pinning `algorithms=["HS256"]`, PyJWT would honor
    the token's own `alg` header and accept a token with no signature at all."""
    forged = jwt.encode(_session_payload(), key="", algorithm="none")
    with pytest.raises(AuthenticationFailed):
        _service().verify_session_token(forged)


def test_an_expired_token_is_rejected() -> None:
    now = int(time.time())
    expired = jwt.encode(
        _session_payload(iat=now - 300, exp=now - 60), _SECRET, algorithm="HS256"
    )
    with pytest.raises(AuthenticationFailed):
        _service().verify_session_token(expired)


@pytest.mark.parametrize("missing", ["tid", "role", "exp"])
def test_a_token_missing_a_required_claim_is_rejected(missing) -> None:
    """`tid` and `role` are what the principal is built from — a token without them
    would produce a KeyError deep in the request, or worse, a default."""
    payload = _session_payload()
    del payload[missing]
    token = jwt.encode(payload, _SECRET, algorithm="HS256")
    with pytest.raises(AuthenticationFailed):
        _service().verify_session_token(token)


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "some-other-api"},
        {"iss": "https://evil.example.com"},
    ],
)
def test_a_token_for_another_audience_or_issuer_is_rejected(overrides) -> None:
    token = jwt.encode(_session_payload(**overrides), _SECRET, algorithm="HS256")
    with pytest.raises(AuthenticationFailed):
        _service().verify_session_token(token)


def test_garbage_is_rejected_as_authentication_failure_not_a_crash() -> None:
    with pytest.raises(AuthenticationFailed):
        _service().verify_session_token("not-a-jwt-at-all")


def test_the_failure_message_never_says_why() -> None:
    """Expired vs. wrong-audience vs. bad-signature tells an attacker which knob to
    turn; the real reason is logged server-side instead."""
    now = int(time.time())
    expired = jwt.encode(_session_payload(iat=now - 300, exp=now - 60), _SECRET, algorithm="HS256")
    forged = jwt.encode(_session_payload(), "b" * 32, algorithm="HS256")

    messages = set()
    for token in (expired, forged):
        with pytest.raises(AuthenticationFailed) as excinfo:
            _service().verify_session_token(token)
        messages.add(str(excinfo.value))
    assert len(messages) == 1


# -- claim -> identity mapping --------------------------------------------------------


def test_identity_is_read_from_the_configured_claims() -> None:
    service = _service()
    tenant_key, email = service._identity_from_claims(
        {"tid": "aad-directory-1", "preferred_username": "someone@example.com"}
    )
    assert (tenant_key, email) == ("aad-directory-1", "someone@example.com")


@pytest.mark.parametrize(
    ("claims", "expected_email"),
    [
        ({"tid": "d1", "preferred_username": "a@x.com", "email": "b@x.com"}, "a@x.com"),
        ({"tid": "d1", "email": "b@x.com"}, "b@x.com"),
        ({"tid": "d1", "sub": "opaque-subject-id"}, "opaque-subject-id"),
    ],
)
def test_email_claim_falls_through_to_email_then_sub(claims, expected_email) -> None:
    """AAD v2.0 only emits preferred_username/email when the app registration asks for
    them; a deployment that hasn't is better off with an ugly-but-stable `sub` identity
    than a login that fails outright."""
    assert _service()._identity_from_claims(claims)[1] == expected_email


def test_a_token_with_no_tenant_claim_is_rejected() -> None:
    with pytest.raises(AuthenticationFailed, match="tid"):
        _service()._identity_from_claims({"preferred_username": "a@x.com"})


def test_a_token_with_no_usable_identity_claim_is_rejected() -> None:
    with pytest.raises(AuthenticationFailed):
        _service()._identity_from_claims({"tid": "d1"})


def test_a_custom_tenant_claim_is_honored() -> None:
    """For a deployment mapping several KubeSandbox tenants onto one AAD directory."""
    service = _service(oidc_tenant_claim="kubesandbox_tenant")
    tenant_key, _ = service._identity_from_claims(
        {"tid": "shared-directory", "kubesandbox_tenant": "acme", "email": "a@acme.com"}
    )
    assert tenant_key == "acme"


# -- first-login provisioning ---------------------------------------------------------


async def test_first_login_provisions_a_tenant_and_user(db_session) -> None:
    service = _service()
    principal = await service.resolve_principal_from_claims(
        {"tid": "directory-1", "preferred_username": "new@example.com"}, db_session
    )

    tenant = (
        await db_session.execute(select(Tenant).where(Tenant.name == "oidc:directory-1"))
    ).scalar_one()
    user = (
        await db_session.execute(select(User).where(User.email == "new@example.com"))
    ).scalar_one()

    assert principal.tenant_id == tenant.id
    assert principal.user_id == user.id
    # A prefixed tenant name so an OIDC-provisioned tenant can never collide with an
    # operator-created one (e.g. `local-dev`).
    assert tenant.name.startswith("oidc:")


async def test_a_new_user_is_never_an_admin(db_session) -> None:
    """Roles are a KubeSandbox concept (doc §11's RBAC). Promotion must be a deliberate
    act by an existing admin, not something an IdP claim can grant — so even a token
    claiming otherwise gets `user`."""
    principal = await _service().resolve_principal_from_claims(
        {"tid": "d1", "email": "a@x.com", "role": "admin", "roles": ["admin"], "wids": ["admin"]},
        db_session,
    )
    assert principal.role == "user"


async def test_second_login_reuses_the_same_tenant_and_user(db_session) -> None:
    service = _service()
    claims = {"tid": "d1", "email": "a@x.com"}

    first = await service.resolve_principal_from_claims(claims, db_session)
    second = await service.resolve_principal_from_claims(claims, db_session)

    assert first == second
    assert len((await db_session.execute(select(User))).scalars().all()) == 1
    assert len((await db_session.execute(select(Tenant))).scalars().all()) == 1


async def test_an_existing_users_role_survives_a_later_login(db_session) -> None:
    """The counterpart to the rule above: a promotion an admin performed must not be
    reset to `user` the next time that person signs in."""
    service = _service()
    claims = {"tid": "d1", "email": "a@x.com"}
    principal = await service.resolve_principal_from_claims(claims, db_session)

    promoted = await db_session.get(User, principal.user_id)
    promoted.role = "admin"
    await db_session.commit()

    assert (await service.resolve_principal_from_claims(claims, db_session)).role == "admin"


async def test_the_same_email_under_a_different_tenant_is_refused(db_session) -> None:
    """`users.email` is globally unique (doc §10.1), so the same address can't belong to
    two tenants. Refused rather than silently re-homed — moving a user between tenants
    would hand them another tenant's sandboxes."""
    service = _service()
    await service.resolve_principal_from_claims({"tid": "d1", "email": "a@x.com"}, db_session)

    with pytest.raises(AuthenticationFailed, match="different tenant"):
        await service.resolve_principal_from_claims({"tid": "d2", "email": "a@x.com"}, db_session)


# -- configuration errors are not authentication errors -------------------------------


async def test_missing_oidc_audience_is_a_configuration_error_not_a_401() -> None:
    """A 503 vs. a 401: an operator chasing a user's credential when the real problem is
    a missing setting is exactly the confusion collapsing these would cause."""
    service = _service(oidc_issuer="https://issuer.example.com", oidc_audience=None)
    with pytest.raises(ConfigurationError, match="oidc_audience"):
        await service.verify_oidc_token("irrelevant")


async def test_missing_issuer_and_jwks_url_is_a_configuration_error() -> None:
    service = _service(oidc_audience="client-id", oidc_issuer=None, oidc_jwks_url=None)
    with pytest.raises(ConfigurationError, match="oidc_issuer"):
        await service.verify_oidc_token("irrelevant")


async def test_an_explicit_jwks_url_skips_discovery() -> None:
    """No network call: if discovery were attempted, this would fail trying to reach
    a nonexistent host rather than returning the configured URL."""
    service = _service(oidc_audience="client-id", oidc_jwks_url="https://example.com/keys")
    assert await service._resolve_jwks_url() == "https://example.com/keys"


def test_jwk_client_is_cached_per_url() -> None:
    """`PyJWKClient` handles key rotation itself; this cache only avoids rebuilding the
    client (and re-fetching the key set) on every login. A `staticmethod` so the cache
    key is the URL and not `self`, which would defeat it — a new AuthService is
    constructed per request."""
    first = AuthService._jwk_client("https://example.com/keys")
    second = AuthService._jwk_client("https://example.com/keys")
    third = AuthService._jwk_client("https://other.example.com/keys")
    assert first is second
    assert first is not third
