from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import Principal, get_entitlement_service, require_admin
from app.services.entitlement_service import EntitlementService

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class EntitlementIn(BaseModel):
    scope: str
    """"tenant" | "user"."""
    scope_id: str
    component_name: str
    version_range: str = "*"
    visible: bool = True


class EntitlementOut(EntitlementIn):
    id: str


class PublishGrantIn(BaseModel):
    scope: str
    scope_id: str
    category: str
    """One of the Component categories, or "template" (doc §3.6)."""
    allowed: bool = True


class PublishGrantOut(PublishGrantIn):
    id: str


@router.get("/entitlements", response_model=list[EntitlementOut])
async def list_entitlements(
    scope: str | None = Query(default=None),
    scope_id: str | None = Query(default=None),
    component_name: str | None = Query(default=None),
    _: Principal = Depends(require_admin),
    service: EntitlementService = Depends(get_entitlement_service),
) -> list[EntitlementOut]:
    rows = await service.list_entitlements(
        scope=scope, scope_id=scope_id, component_name=component_name
    )
    return [
        EntitlementOut(
            id=row.id,
            scope=row.scope,
            scope_id=row.scope_id,
            component_name=row.component_name,
            version_range=row.version_range,
            visible=row.visible,
        )
        for row in rows
    ]


@router.patch("/entitlements", response_model=EntitlementOut)
async def upsert_entitlement(
    body: EntitlementIn,
    _: Principal = Depends(require_admin),
    service: EntitlementService = Depends(get_entitlement_service),
) -> EntitlementOut:
    row = await service.upsert_entitlement(
        scope=body.scope,
        scope_id=body.scope_id,
        component_name=body.component_name,
        version_range=body.version_range,
        visible=body.visible,
    )
    return EntitlementOut(
        id=row.id,
        scope=row.scope,
        scope_id=row.scope_id,
        component_name=row.component_name,
        version_range=row.version_range,
        visible=row.visible,
    )


@router.get("/publish-grants", response_model=list[PublishGrantOut])
async def list_publish_grants(
    scope: str | None = Query(default=None),
    scope_id: str | None = Query(default=None),
    category: str | None = Query(default=None),
    _: Principal = Depends(require_admin),
    service: EntitlementService = Depends(get_entitlement_service),
) -> list[PublishGrantOut]:
    rows = await service.list_publish_grants(scope=scope, scope_id=scope_id, category=category)
    return [
        PublishGrantOut(
            id=row.id, scope=row.scope, scope_id=row.scope_id, category=row.category,
            allowed=row.allowed,
        )
        for row in rows
    ]


@router.patch("/publish-grants", response_model=PublishGrantOut)
async def upsert_publish_grant(
    body: PublishGrantIn,
    _: Principal = Depends(require_admin),
    service: EntitlementService = Depends(get_entitlement_service),
) -> PublishGrantOut:
    row = await service.upsert_publish_grant(
        scope=body.scope, scope_id=body.scope_id, category=body.category, allowed=body.allowed
    )
    return PublishGrantOut(
        id=row.id, scope=row.scope, scope_id=row.scope_id, category=row.category,
        allowed=row.allowed,
    )
