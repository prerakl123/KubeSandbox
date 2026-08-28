"""FastAPI dependency providers: auth principal, registry, provisioner, SandboxService.

Two credential types are accepted, in this order (doc §11):

1. `Authorization: Bearer <session-jwt>` — a standalone human user / the UI, holding a
   short-lived KubeSandbox session token issued by `POST /v1/auth/token` after an OIDC
   login. Verified locally (signature only, no I/O), so it costs less than the API-key
   path despite carrying more information.
2. `X-API-Key: <key>` — a service account / the workflow-builder. Hashed lookup against
   `api_keys`.

Bearer is checked first because it's the cheaper of the two and the one a UI sends on
every request; a caller presenting both gets the bearer identity. `auth.disabled`
(local only, refused elsewhere by `Settings`) short-circuits both.
"""

from __future__ import annotations

import contextlib
import hashlib
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis
from fastapi import Depends, Header, HTTPException, Request, WebSocket, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cloud.registry import ImageRegistryProvider
from app.cloud.storage import ObjectStorageProvider
from app.core.config import get_settings
from app.domain.auth import Principal
from app.extensions.loader import Registry
from app.persistence.db import get_session, get_session_factory
from app.persistence.models import ApiKey, Tenant, User
from app.provisioners.base import Provisioner
from app.services.auth_service import AuthenticationFailed, AuthService
from app.services.billing_service import BillingService
from app.services.build_manager import BuildManager
from app.services.entitlement_service import EntitlementService
from app.services.pool_manager import PoolManager
from app.services.registry_service import RegistryService
from app.services.sandbox_service import SandboxService
from app.services.template_service import TemplateService
from app.services.weight_class_scheduler import WeightClassScheduler
from app.services.workspace_service import WorkspaceService

_weight_class_scheduler: WeightClassScheduler | None = None


def _get_weight_class_scheduler() -> WeightClassScheduler:
    # Module-level singleton, not per-request: an asyncio.Semaphore's whole point here
    # is capping concurrency *across* requests within this one process (doc §7 — local
    # always runs exactly 1 replica, so "this process" is the whole deployment).
    # Constructing a fresh one per request would cap nothing.
    global _weight_class_scheduler
    if _weight_class_scheduler is None:
        _weight_class_scheduler = WeightClassScheduler(
            heavy_max_concurrent=get_settings().pool.heavy_max_concurrent
        )
    return _weight_class_scheduler

# Local-dev-only fallback identity, used only when auth.disabled (which config.py
# refuses outside app_env=local — see app/core/config.py's model_validator).
_LOCAL_DEV_TENANT_NAME = "local-dev"
_LOCAL_DEV_USER_EMAIL = "local-dev@kubesandbox.local"


_LAST_USED_WRITE_INTERVAL = timedelta(minutes=1)
"""Coalescing window for `api_keys.last_used_at` writes — see `_principal_from_api_key`.
A minute's resolution is plenty for "is this key still in use?" and turns a per-request
UPDATE into an occasional one."""


def _is_stale(last_used_at: datetime | None, now: datetime) -> bool:
    """Whether `last_used_at` is old enough to be worth rewriting.

    The tz normalization is not cosmetic: a `DateTime(timezone=True)` column comes back
    *aware* from Postgres but *naive* from SQLite (which has no native timestamptz), and
    subtracting the two raises `TypeError`. Since this runs on the authentication path,
    an unguarded comparison would turn a perfectly valid API key into a 500 on any
    backend that returns naive values — and the surrounding `suppress(SQLAlchemyError)`
    would not catch a TypeError. Assuming UTC for a naive value is correct here: every
    write to this column is `datetime.now(UTC)`.
    """
    if last_used_at is None:
        return True
    if last_used_at.tzinfo is None:
        last_used_at = last_used_at.replace(tzinfo=UTC)
    return (now - last_used_at) > _LAST_USED_WRITE_INTERVAL


def _hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def _get_or_create_local_dev_principal(session: AsyncSession) -> Principal:
    tenant = (
        await session.execute(select(Tenant).where(Tenant.name == _LOCAL_DEV_TENANT_NAME))
    ).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name=_LOCAL_DEV_TENANT_NAME)
        session.add(tenant)
        await session.flush()

    user = (
        await session.execute(select(User).where(User.email == _LOCAL_DEV_USER_EMAIL))
    ).scalar_one_or_none()
    if user is None:
        user = User(tenant_id=tenant.id, email=_LOCAL_DEV_USER_EMAIL, role="admin")
        session.add(user)
        await session.flush()

    return Principal(tenant_id=tenant.id, user_id=user.id, role=user.role)


def _bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an `Authorization: Bearer <token>` header, case-insensitively
    on the scheme (RFC 6750 says the scheme is case-insensitive, and real clients differ)."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def _principal_from_api_key(session: AsyncSession, api_key: str) -> Principal:
    row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == _hash_api_key(api_key), ApiKey.revoked.is_(False))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked API key")

    # Best-effort `last_used_at` bookkeeping (Phase 9): it's what makes "can I safely
    # revoke this key?" answerable in the UI. Only written when the stamp is more than
    # a minute stale, so a workflow-builder hammering /v1/execute doesn't turn every
    # request into a write — and wrapped in a suppress because a failed bookkeeping
    # write must never turn a valid credential into a 500.
    now = datetime.now(UTC)
    if _is_stale(row.last_used_at, now):
        with contextlib.suppress(SQLAlchemyError):
            row.last_used_at = now
            await session.commit()

    # `user_id=None` and `role="service"` on purpose: an API key belongs to a tenant,
    # not a person (doc §11's "service accounts"), so anything user-scoped — a
    # persistent workspace, a credit request's requester — has no user to attribute to.
    return Principal(tenant_id=row.tenant_id, user_id=None, role="service")


async def _resolve_principal(
    session: AsyncSession, api_key: str | None, bearer: str | None = None
) -> Principal:
    settings = get_settings()

    if settings.auth.disabled:
        principal = await _get_or_create_local_dev_principal(session)
        await session.commit()
        return principal

    if bearer:
        try:
            return AuthService(settings.auth).verify_session_token(bearer)
        except AuthenticationFailed as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    if api_key:
        return await _principal_from_api_key(session, api_key)

    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "missing credentials: send 'Authorization: Bearer <session token>' or 'X-API-Key: <key>'",
    )


async def get_current_principal(
    session: AsyncSession = Depends(get_session),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> Principal:
    return await _resolve_principal(session, x_api_key, _bearer_token(authorization))


async def get_ws_principal(
    websocket: WebSocket,
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """WS counterpart to get_current_principal.

    Browsers can't set headers on a WebSocket handshake, so the credential travels in
    the query string (doc §5.2's WS attach contract): `?access_token=` for a UI session
    token, or `?api_key=` for a service account. A session token is much the better
    thing to put in a URL — it expires in an hour, where an API key doesn't — which is
    the other reason `/v1/auth/token` issues our own token rather than the API
    validating the IdP's on every call.

    Shares `_resolve_principal` so `auth.disabled` behavior, bearer precedence, and
    API-key hashing can never drift between the HTTP and WS paths.
    """
    params = websocket.query_params
    # Also accepts a real Authorization header, for non-browser WS clients (the SDK's
    # own attach helper, `websocat`) that can set one.
    bearer = params.get("access_token") or _bearer_token(websocket.headers.get("authorization"))
    return await _resolve_principal(session, params.get("api_key"), bearer)


async def require_admin(principal: Principal = Depends(get_current_principal)) -> Principal:
    """Admin-only endpoints bypass entitlement filtering entirely (doc §3.6) — this
    dependency is the gate, raised as 403 before the route body ever runs."""
    if principal.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return principal


def get_registry(request: Request) -> Registry:
    return request.app.state.registry


def get_provisioner(request: Request) -> Provisioner:
    return request.app.state.provisioner


def get_redis(request: Request) -> redis.Redis:
    return request.app.state.redis


def get_redis_ws(websocket: WebSocket) -> redis.Redis:
    return websocket.app.state.redis


def get_provisioner_ws(websocket: WebSocket) -> Provisioner:
    return websocket.app.state.provisioner


def get_auth_service() -> AuthService:
    return AuthService(get_settings().auth)


def get_billing_service() -> BillingService:
    return BillingService(default_mode=get_settings().billing.default_mode)


def _build_sandbox_service(registry: Registry, provisioner: Provisioner) -> SandboxService:
    settings = get_settings()
    pool_manager = PoolManager(provisioner) if settings.pool.enabled else None
    workspace_service = (
        WorkspaceService(default_quota_mb=settings.workspace.default_quota_mb)
        if settings.workspace.persistence_enabled
        else None
    )
    billing_service = get_billing_service() if settings.billing.enabled else None
    return SandboxService(
        registry,
        provisioner,
        pool_manager=pool_manager,
        default_idle_ttl_seconds=settings.ttl.default_idle_seconds,
        default_max_ttl_seconds=settings.ttl.default_max_seconds,
        weight_class_scheduler=_get_weight_class_scheduler(),
        heavy_node_selector=settings.provisioner.heavy_node_selector,
        heavy_tolerations=settings.provisioner.heavy_tolerations,
        workspace_service=workspace_service,
        billing_service=billing_service,
        # Needed only by the `?async=true` path, whose work outlives the request that
        # triggered it and so can't use the request-scoped session (the same reason
        # BuildManager takes one).
        session_factory=get_session_factory(),
    )


def get_sandbox_service(
    registry: Registry = Depends(get_registry),
    provisioner: Provisioner = Depends(get_provisioner),
) -> SandboxService:
    return _build_sandbox_service(registry, provisioner)


def get_sandbox_service_ws(websocket: WebSocket) -> SandboxService:
    return _build_sandbox_service(websocket.app.state.registry, websocket.app.state.provisioner)


def get_entitlement_service(session: AsyncSession = Depends(get_session)) -> EntitlementService:
    return EntitlementService(session)


def get_registry_service(
    registry: Registry = Depends(get_registry),
    session: AsyncSession = Depends(get_session),
    entitlements: EntitlementService = Depends(get_entitlement_service),
) -> RegistryService:
    return RegistryService(registry, session, entitlements)


def get_template_service(
    registry: Registry = Depends(get_registry),
    session: AsyncSession = Depends(get_session),
    entitlements: EntitlementService = Depends(get_entitlement_service),
) -> TemplateService:
    return TemplateService(registry, session, entitlements)


def get_image_registry_provider(request: Request) -> ImageRegistryProvider:
    return request.app.state.image_registry_provider


def get_object_storage_provider(request: Request) -> ObjectStorageProvider | None:
    return request.app.state.object_storage_provider


def get_build_manager(
    registry: Registry = Depends(get_registry),
    entitlements: EntitlementService = Depends(get_entitlement_service),
    image_registry: ImageRegistryProvider = Depends(get_image_registry_provider),
    object_storage: ObjectStorageProvider | None = Depends(get_object_storage_provider),
) -> BuildManager:
    # run_build() executes as a FastAPI BackgroundTask after the triggering request
    # has already returned, so it needs its own session — get_session_factory() (not
    # get_session, which is request-scoped) is exactly what BuildManager uses for that.
    return BuildManager(registry, entitlements, image_registry, object_storage, get_session_factory())
