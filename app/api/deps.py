"""FastAPI dependency providers: auth principal, registry, provisioner, SandboxService.

Auth is a Phase 1 skeleton: hashed API keys only. OIDC/JWT user sessions (doc §11) are
a later phase — nothing here blocks adding them alongside API keys.
"""

from __future__ import annotations

import hashlib

import redis.asyncio as redis
from fastapi import Depends, Header, HTTPException, Request, WebSocket, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cloud.registry import ImageRegistryProvider
from app.cloud.storage import ObjectStorageProvider
from app.core.config import get_settings
from app.domain.auth import Principal
from app.extensions.loader import Registry
from app.persistence.db import get_session, get_session_factory
from app.persistence.models import ApiKey, Tenant, User
from app.provisioners.base import Provisioner
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


async def _resolve_principal(session: AsyncSession, api_key: str | None) -> Principal:
    settings = get_settings()

    if settings.auth.disabled:
        principal = await _get_or_create_local_dev_principal(session)
        await session.commit()
        return principal

    if not api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing API key")

    row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == _hash_api_key(api_key), ApiKey.revoked.is_(False))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked API key")

    return Principal(tenant_id=row.tenant_id, user_id=None, role="service")


async def get_current_principal(
    session: AsyncSession = Depends(get_session),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Principal:
    return await _resolve_principal(session, x_api_key)


async def get_ws_principal(
    websocket: WebSocket,
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """WS counterpart to get_current_principal: browsers can't set custom headers on a
    WebSocket handshake, so the API key travels as a `?api_key=` query param instead
    (doc §5.2's WS attach contract). Shares `_resolve_principal` so `auth.disabled`
    local-dev behavior and API-key hashing/lookup never drift between the two."""
    return await _resolve_principal(session, websocket.query_params.get("api_key"))


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


def _build_sandbox_service(registry: Registry, provisioner: Provisioner) -> SandboxService:
    settings = get_settings()
    pool_manager = PoolManager(provisioner) if settings.pool.enabled else None
    workspace_service = (
        WorkspaceService(default_quota_mb=settings.workspace.default_quota_mb)
        if settings.workspace.persistence_enabled
        else None
    )
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
