"""OIDC login + KubeSandbox session tokens (doc §11, the last open Phase 0 item).

**Why two token types.** Doc §11 says "OIDC (Azure AD) -> short-lived JWT session", and
that arrow is the whole design: the IdP's own token is validated exactly *once*, at
`POST /v1/auth/token`, and exchanged for a KubeSandbox-issued HS256 session token that
every subsequent request carries. The alternative — validating the AAD token on every
request — means an RS256 verification plus a `users` lookup per call, and gives the
control plane no place to put the resolved `tenant_id`/`user_id`/`role`. Issuing our own
token puts all three *in* the token, so `get_current_principal` is a local signature
check with no database round trip at all.

It also makes the WebSocket path safe. A browser can't set headers on a WS handshake, so
the credential has to ride in the query string (doc §5.2); a 1-hour session token
leaking into a proxy log is a bounded problem, a long-lived API key is not.

**What is deliberately not here.** No refresh-token flow: the IdP already holds the
long-lived session, so a UI renews by re-running its own silent-auth and calling
`/v1/auth/token` again. No password/local-account path — there is no credential store in
this system by design, and adding one would make it the weakest link in doc §6's
otherwise structural security story.
"""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AuthSettings
from app.core.errors import ConfigurationError, KubeSandboxError
from app.core.logging import get_logger
from app.domain.auth import Principal
from app.persistence.models import Tenant, User

logger = get_logger(__name__)

_SESSION_ALGORITHM = "HS256"
_SESSION_ISSUER = "kubesandbox"
_SESSION_AUDIENCE = "kubesandbox-api"

_OIDC_ALGORITHMS = ["RS256", "RS384", "RS512"]
"""Asymmetric only, and pinned. Accepting HS256 here would be the classic algorithm
confusion vulnerability: an attacker could sign a token with the *public* key (or, worse,
with our own `jwt_secret`) and have it verified as if the IdP had issued it."""

_TENANT_NAME_PREFIX = "oidc:"
"""`tenants` has no external-id column of its own (doc §10.1's schema), so an OIDC
directory is mapped onto a Tenant row by a prefixed `name`. Prefixed rather than bare so
an OIDC-provisioned tenant can never collide with an operator-created one that happens
to share the same string (e.g. the `local-dev` tenant)."""


class AuthenticationFailed(KubeSandboxError):
    """A credential was present but not valid. Mapped to 401 by the API layer.

    Every failure mode collapses to this one type with a deliberately vague message —
    expired vs. wrong-audience vs. unknown-key tells an attacker which knob to turn,
    and the real reason is logged server-side instead.
    """


@dataclass(frozen=True)
class SessionToken:
    access_token: str
    expires_at: datetime
    principal: Principal

    @property
    def expires_in(self) -> int:
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))


class AuthService:
    """Stateless apart from the JWKS cache — constructed per request from settings, the
    same shape as `BillingService`."""

    def __init__(self, settings: AuthSettings) -> None:
        self._settings = settings

    # -- OIDC discovery / JWKS --------------------------------------------------------

    @staticmethod
    @functools.lru_cache(maxsize=4)
    def _jwk_client(jwks_url: str) -> jwt.PyJWKClient:
        """Cached per JWKS URL for the process lifetime.

        `PyJWKClient` does its own key caching with a 5-minute lifespan, which is what
        handles the IdP rotating signing keys; the `lru_cache` here only avoids
        rebuilding the client (and re-fetching the key set) on every single login. A
        `staticmethod` so the cache key is the URL and not `self`, which would defeat
        it entirely — a new `AuthService` is constructed per request.
        """
        return jwt.PyJWKClient(jwks_url, cache_keys=True)

    async def _resolve_jwks_url(self) -> str:
        if self._settings.oidc_jwks_url:
            return self._settings.oidc_jwks_url
        issuer = self._settings.oidc_issuer
        if not issuer:
            raise ConfigurationError(
                "auth.oidc_issuer (or auth.oidc_jwks_url) must be set to accept OIDC logins"
            )
        discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(discovery_url)
            response.raise_for_status()
            document = response.json()
        jwks_uri = document.get("jwks_uri")
        if not jwks_uri:
            raise ConfigurationError(f"OIDC discovery document at {discovery_url} has no jwks_uri")
        return jwks_uri

    async def verify_oidc_token(self, token: str) -> dict[str, Any]:
        """Validate an IdP-issued id/access token and return its claims.

        Raises `AuthenticationFailed` for anything wrong with the token itself, and
        `ConfigurationError` for anything wrong with *our* configuration — the two are
        genuinely different failures (a 401 vs. a 500) and collapsing them would have an
        operator hunting a user's credential when the real problem is a missing setting.
        """
        if not self._settings.oidc_audience:
            raise ConfigurationError("auth.oidc_audience must be set to accept OIDC logins")
        jwks_url = await self._resolve_jwks_url()
        client = self._jwk_client(jwks_url)
        try:
            # PyJWKClient is synchronous (urllib) — off-thread so a JWKS fetch on a cold
            # cache can't block the event loop for every other in-flight request.
            signing_key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=_OIDC_ALGORITHMS,
                audience=self._settings.oidc_audience,
                issuer=self._settings.oidc_issuer,
                options={"require": ["exp", "iat"]},
            )
        except jwt.PyJWTError as exc:
            logger.warning("oidc_token_rejected", error=str(exc), error_type=type(exc).__name__)
            raise AuthenticationFailed("invalid OIDC token") from exc

    # -- user/tenant provisioning -----------------------------------------------------

    def _identity_from_claims(self, claims: dict[str, Any]) -> tuple[str, str]:
        """(tenant_key, email) — the two things a local identity is keyed on."""
        tenant_key = claims.get(self._settings.oidc_tenant_claim)
        if not tenant_key:
            raise AuthenticationFailed(
                f"token has no {self._settings.oidc_tenant_claim!r} claim to resolve a tenant from"
            )
        # Falls through `email` and finally `sub`: AAD v2.0 only emits
        # `preferred_username`/`email` when the app registration asks for them, and a
        # deployment that hasn't is better off with an ugly-but-stable `sub` identity
        # than a login that fails outright.
        email = (
            claims.get(self._settings.oidc_email_claim)
            or claims.get("email")
            or claims.get("sub")
        )
        if not email:
            raise AuthenticationFailed("token has no usable identity claim (preferred_username/email/sub)")
        return str(tenant_key), str(email)

    async def resolve_principal_from_claims(self, claims: dict[str, Any], session: AsyncSession) -> Principal:
        """Look up — or provision on first login — the Tenant and User behind a set of
        validated OIDC claims.

        A newly provisioned user always gets role `user`, never `admin`, regardless of
        anything in the token: role is a KubeSandbox concept (doc §11's RBAC) and
        promoting someone must be a deliberate act by an existing admin, not something
        an IdP claim can grant. An *existing* user's role is never overwritten either,
        so a promotion survives every subsequent login.
        """
        tenant_key, email = self._identity_from_claims(claims)
        tenant_name = f"{_TENANT_NAME_PREFIX}{tenant_key}"

        tenant = (
            await session.execute(select(Tenant).where(Tenant.name == tenant_name))
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(name=tenant_name)
            session.add(tenant)
            await session.flush()
            logger.info("oidc_tenant_provisioned", tenant_id=tenant.id, tenant_key=tenant_key)

        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(tenant_id=tenant.id, email=email, role="user")
            session.add(user)
            await session.flush()
            logger.info("oidc_user_provisioned", user_id=user.id, tenant_id=tenant.id)
        elif user.tenant_id != tenant.id:
            # `users.email` is globally unique (doc §10.1's schema), so the same address
            # can't belong to two tenants. Refused rather than silently re-homed: moving
            # a user between tenants would hand them another tenant's sandboxes.
            raise AuthenticationFailed("this identity is already registered under a different tenant")

        await session.commit()
        return Principal(tenant_id=user.tenant_id, user_id=user.id, role=user.role)

    # -- session tokens ---------------------------------------------------------------

    def issue_session_token(self, principal: Principal) -> SessionToken:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._settings.session_ttl_seconds)
        payload = {
            "iss": _SESSION_ISSUER,
            "aud": _SESSION_AUDIENCE,
            "sub": principal.user_id or "",
            "tid": principal.tenant_id,
            "role": principal.role,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        token = jwt.encode(payload, self._settings.jwt_secret, algorithm=_SESSION_ALGORITHM)
        return SessionToken(access_token=token, expires_at=expires_at, principal=principal)

    def verify_session_token(self, token: str) -> Principal:
        """Local signature check — no I/O, no database. This is the hot path: every
        authenticated UI request goes through it."""
        try:
            payload = jwt.decode(
                token,
                self._settings.jwt_secret,
                # Pinned to HS256 alone. Without `algorithms`, PyJWT would honor the
                # token's own `alg` header, and `alg: none` or an RS256 token verified
                # against our secret-as-public-key are both real attacks.
                algorithms=[_SESSION_ALGORITHM],
                audience=_SESSION_AUDIENCE,
                issuer=_SESSION_ISSUER,
                options={"require": ["exp", "iat", "tid", "role"]},
            )
        except jwt.PyJWTError as exc:
            logger.warning("session_token_rejected", error=str(exc), error_type=type(exc).__name__)
            raise AuthenticationFailed("invalid or expired session token") from exc
        return Principal(
            tenant_id=payload["tid"],
            user_id=payload["sub"] or None,
            role=payload["role"],
        )

    async def login(self, oidc_token: str, session: AsyncSession) -> SessionToken:
        """The whole exchange: validate the IdP's token, resolve/provision the local
        identity, hand back a session token."""
        claims = await self.verify_oidc_token(oidc_token)
        principal = await self.resolve_principal_from_claims(claims, session)
        return self.issue_session_token(principal)
