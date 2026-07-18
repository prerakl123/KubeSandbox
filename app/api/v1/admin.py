from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import Principal, get_entitlement_service, require_admin
from app.services.entitlement_service import EntitlementService

router = APIRouter(prefix="/v1/admin", tags=["Admin"])


class EntitlementIn(BaseModel):
    scope: str = Field(description="'tenant' or 'user'.")
    scope_id: str = Field(description="The tenant id or user id this entitlement applies to.")
    component_name: str = Field(description="Bare component name (not versioned) this entitlement covers.")
    version_range: str = Field(default="*", description="'*' (any version) or an exact version string.")
    visible: bool = Field(default=True, description="Whether this scope may see/select the component.")


class EntitlementOut(EntitlementIn):
    id: str


class PublishGrantIn(BaseModel):
    scope: str = Field(description="'tenant' or 'user'.")
    scope_id: str = Field(description="The tenant id or user id this grant applies to.")
    category: str = Field(description="One of the Component categories, or 'template' (doc §3.6).")
    allowed: bool = Field(default=True, description="Whether this scope may publish its own private components/templates in this category.")


class PublishGrantOut(PublishGrantIn):
    id: str


@router.get(
    "/entitlements",
    response_model=list[EntitlementOut],
    summary="List catalog entitlements",
    description="What a tenant/user scope may see and select from the component catalog (doc §3.6). Admin-only.",
)
async def list_entitlements(
    scope: str | None = Query(default=None, description="Filter to 'tenant' or 'user' scoped entries."),
    scope_id: str | None = Query(default=None, description="Filter to one tenant id or user id."),
    component_name: str | None = Query(default=None, description="Filter to one component name."),
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


@router.patch(
    "/entitlements",
    response_model=EntitlementOut,
    summary="Set a catalog entitlement",
    description="Create or update what a tenant/user scope may see and select (doc §3.6). Admin-only.",
)
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


@router.get(
    "/publish-grants",
    response_model=list[PublishGrantOut],
    summary="List publish grants",
    description="Who may publish their own private components/templates, and in which category (doc §3.6). Admin-only.",
)
async def list_publish_grants(
    scope: str | None = Query(default=None, description="Filter to 'tenant' or 'user' scoped entries."),
    scope_id: str | None = Query(default=None, description="Filter to one tenant id or user id."),
    category: str | None = Query(default=None, description="Filter to one category, or 'template'."),
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


@router.patch(
    "/publish-grants",
    response_model=PublishGrantOut,
    summary="Set a publish grant",
    description="Create or update whether a tenant/user scope may publish its own private components/templates in a category (doc §3.6). Admin-only.",
)
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
