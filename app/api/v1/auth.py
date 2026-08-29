"""Authentication + identity endpoints (doc §11) — the surface a browser UI logs in
against and reads itself back from.

Three endpoints, each answering a question the UI cannot proceed without:

* `GET /v1/auth/config` — "which IdP do I send the user to?" Unauthenticated by
  necessity (it's what you call *before* you have a credential) and safe: everything it
  returns is public by construction, since an OIDC issuer and client id are baked into
  every SPA bundle that has ever shipped.
* `POST /v1/auth/token` — "here's the token my MSAL flow got; give me a session."
* `GET /v1/me` — "who am I, what may I do, and which features are on in this
  deployment?" A UI needs all three to decide what to even render.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    Principal,
    get_audit_service,
    get_auth_service,
    get_current_principal,
    get_quota_service,
)
from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.persistence.db import get_session
from app.persistence.models import User
from app.services import audit_service as audit
from app.services.audit_service import AuditService
from app.services.auth_service import AuthenticationFailed, AuthService
from app.services.quota_service import QuotaService

router = APIRouter(prefix="/v1", tags=["Auth"])


class AuthConfigResponse(BaseModel):
    """Everything a frontend needs to start its own OIDC flow, so the SPA doesn't
    hardcode per-environment values and drift from what the API actually validates."""

    auth_required: bool = Field(
        description="False only in local dev (`auth.disabled`), where every request is "
        "already the local admin principal and the UI can skip login entirely."
    )
    provider: str = Field(description="'oidc' when an issuer is configured, else 'none'.")
    issuer: str | None = Field(description="OIDC issuer to authenticate against.")
    client_id: str | None = Field(description="Client id the UI should present to the IdP.")
    scopes: list[str] = Field(
        description="Scopes to request. 'openid'/'profile'/'email' are the minimum that "
        "yield the identity claims this API resolves a user from."
    )
    session_ttl_seconds: int = Field(
        description="Lifetime of the session token `POST /v1/auth/token` returns, so a "
        "UI can schedule a silent re-login before it lapses rather than discovering "
        "expiry as a 401 mid-action."
    )


class TokenRequest(BaseModel):
    oidc_token: str = Field(
        description="The id token (or access token, if its audience is this API) that "
        "the UI's own OIDC/MSAL flow obtained from the IdP. Validated against the "
        "issuer's JWKS exactly once, here."
    )


class PrincipalResponse(BaseModel):
    tenant_id: str
    user_id: str | None = Field(
        description="Null for a service-account (API-key) caller — a key belongs to a "
        "tenant, not a person, so nothing user-scoped can be attributed to it."
    )
    role: str = Field(description="admin | operator | user | service (doc §11's RBAC).")
    email: str | None = Field(description="Null for a service-account caller.")


class TokenResponse(BaseModel):
    access_token: str = Field(description="Send as `Authorization: Bearer <token>`.")
    token_type: str = "Bearer"
    expires_in: int = Field(description="Seconds until expiry.")
    principal: PrincipalResponse


class FeatureFlags(BaseModel):
    """Which optional subsystems are actually on in *this* deployment.

    Every one of these is opt-in config (doc §4.3/§10.2/§13), and a UI that renders a
    "persistent workspace" toggle or a credit balance against a deployment where the
    feature is off produces buttons that only ever return 400. Better to ask once.
    """

    persistent_workspaces: bool
    billing: bool
    pooling: bool
    interactive_attach: bool = Field(
        default=True,
        description="Always true — PTY attach has no feature flag; included so a UI can "
        "treat this object as the single source of truth for capability checks rather "
        "than special-casing one of them.",
    )


class MeResponse(BaseModel):
    principal: PrincipalResponse
    features: FeatureFlags
    app_env: str = Field(description="local | aks-prod (doc §7's two environments).")


def _principal_response(principal: Principal, email: str | None) -> PrincipalResponse:
    return PrincipalResponse(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        role=principal.role,
        email=email,
    )


def _features(settings: Settings) -> FeatureFlags:
    return FeatureFlags(
        persistent_workspaces=settings.workspace.persistence_enabled,
        billing=settings.billing.enabled,
        pooling=settings.pool.enabled,
    )


@router.get(
    "/auth/config",
    response_model=AuthConfigResponse,
    summary="Public auth configuration for a frontend",
    description=(
        "What a UI needs to start an OIDC login (doc §11). Unauthenticated by "
        "necessity — it is what a client calls before it has any credential — and "
        "returns nothing secret: an issuer URL and a public client id."
    ),
)
async def auth_config() -> AuthConfigResponse:
    settings = get_settings()
    auth = settings.auth
    return AuthConfigResponse(
        auth_required=not auth.disabled,
        provider="oidc" if auth.oidc_issuer else "none",
        issuer=auth.oidc_issuer,
        client_id=auth.oidc_client_id or auth.oidc_audience,
        scopes=["openid", "profile", "email"],
        session_ttl_seconds=auth.session_ttl_seconds,
    )


@router.post(
    "/auth/token",
    response_model=TokenResponse,
    summary="Exchange an OIDC token for a KubeSandbox session token",
    description=(
        "Validates the IdP's token against the issuer's JWKS, provisions the tenant/user "
        "on first login, and returns a short-lived KubeSandbox session token (doc §11's "
        "\"OIDC -> short-lived JWT session\"). A new user is always created with role "
        "`user`; roles are a KubeSandbox concept and no IdP claim can grant `admin`."
    ),
    responses={
        401: {"description": "The OIDC token is invalid, expired, or for another audience."},
        503: {"description": "OIDC is not configured in this deployment."},
    },
)
async def exchange_token(
    body: TokenRequest,
    session: AsyncSession = Depends(get_session),
    auth_service: AuthService = Depends(get_auth_service),
    audit_svc: AuditService = Depends(get_audit_service),
) -> TokenResponse:
    try:
        token = await auth_service.login(body.oidc_token, session)
    except AuthenticationFailed as exc:
        # Standalone: `login()` already rolled back, and a failed login has no tenant to
        # attribute to. Recorded because a burst of these is the clearest signal of a
        # credential-stuffing attempt against the exchange endpoint.
        await audit_svc.record_standalone(action=audit.AUTH_LOGIN_FAILED, detail={"reason": str(exc)})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except ConfigurationError as exc:
        # 503, not 500: the deployment is missing configuration, which is an
        # operator-fixable state rather than a bug, and a UI should show "login
        # unavailable" rather than "something broke".
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    email: str | None = None
    if token.principal.user_id is not None:
        user = await session.get(User, token.principal.user_id)
        email = user.email if user is not None else None

    # `login()` has already committed the tenant/user provisioning, so this is its own
    # write rather than a join onto that transaction.
    await audit_svc.record_standalone(
        action=audit.AUTH_LOGIN,
        principal=token.principal,
        target=token.principal.user_id,
        detail={"role": token.principal.role},
    )

    return TokenResponse(
        access_token=token.access_token,
        expires_in=token.expires_in,
        principal=_principal_response(token.principal, email),
    )


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Who am I, and what's enabled here",
    description=(
        "The caller's resolved identity plus which optional subsystems this deployment "
        "has turned on. A UI should call this once after login and drive its navigation "
        "from it — rendering a persistent-workspace or billing surface against a "
        "deployment where that feature is off only produces controls that 400."
    ),
)
async def me(
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    settings = get_settings()
    email: str | None = None
    if principal.user_id is not None:
        user = await session.get(User, principal.user_id)
        email = user.email if user is not None else None
    return MeResponse(
        principal=_principal_response(principal, email),
        features=_features(settings),
        app_env=settings.app_env,
    )


class MyQuotaResponse(BaseModel):
    """The caller's own tenant quota position — the read a UI needs to render "3 of 10
    sandboxes" without being an admin.

    Read-only by definition: a tenant seeing its own ceiling is useful, a tenant able to
    raise it is not. Changing quotas is `PATCH /v1/admin/tenants/{id}/quota`.
    """

    enabled: bool = Field(
        description="False means quotas are recorded but not enforced in this deployment — "
        "a UI should not render a limit as binding when it isn't."
    )
    max_concurrent_sandboxes: int | None = Field(description="Null = no limit.")
    max_monthly_minutes: int | None
    concurrent_sandboxes: int
    monthly_minutes: int


@router.get(
    "/me/quota",
    response_model=MyQuotaResponse,
    summary="The caller's own quota position",
    description=(
        "Concurrency and monthly-minute usage against this tenant's ceilings (doc §11). "
        "Deliberately narrower than the admin view: cpu/memory ceilings are reported to "
        "admins but omitted here, because their accounting is an approximation over "
        "per-weight-class budgets and showing a user a precise-looking number they can't "
        "reconcile against anything is worse than not showing it."
    ),
)
async def my_quota(
    principal: Principal = Depends(get_current_principal),
    quota_service: QuotaService = Depends(get_quota_service),
    session: AsyncSession = Depends(get_session),
) -> MyQuotaResponse:
    usage = await quota_service.usage(principal.tenant_id, session=session)
    await session.commit()  # persists the lazily-created row on first look
    return MyQuotaResponse(
        enabled=get_settings().quota.enabled,
        max_concurrent_sandboxes=usage.max_concurrent_sandboxes,
        max_monthly_minutes=usage.max_monthly_minutes,
        concurrent_sandboxes=usage.concurrent_sandboxes,
        monthly_minutes=usage.monthly_minutes,
    )
