"""FastAPI dependency providers: auth principal, registry, provisioner, SandboxService.

Auth is a Phase 1 skeleton: hashed API keys only. OIDC/JWT user sessions (doc §11) are
a later phase — nothing here blocks adding them alongside API keys.
"""

from __future__ import annotations

import hashlib

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.auth import Principal
from app.extensions.loader import Registry
from app.persistence.db import get_session
from app.persistence.models import ApiKey, Tenant, User
from app.provisioners.base import Provisioner
from app.services.entitlement_service import EntitlementService
from app.services.registry_service import RegistryService
from app.services.sandbox_service import SandboxService
from app.services.template_service import TemplateService

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


async def get_current_principal(
    session: AsyncSession = Depends(get_session),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Principal:
    settings = get_settings()

    if settings.auth.disabled:
        principal = await _get_or_create_local_dev_principal(session)
        await session.commit()
        return principal

    if not x_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing X-API-Key header")

    api_key = (
        await session.execute(
            select(ApiKey).where(
                ApiKey.key_hash == _hash_api_key(x_api_key), ApiKey.revoked.is_(False)
            )
        )
    ).scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked API key")

    return Principal(tenant_id=api_key.tenant_id, user_id=None, role="service")


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


def get_sandbox_service(
    registry: Registry = Depends(get_registry),
    provisioner: Provisioner = Depends(get_provisioner),
) -> SandboxService:
    return SandboxService(registry, provisioner)


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
