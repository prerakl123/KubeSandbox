from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import Principal, get_current_principal, get_registry_service
from app.domain.manifests import Component
from app.extensions.loader import COMPONENT_SCHEMA
from app.services.registry_service import RegistryService

router = APIRouter(prefix="/v1/components", tags=["components"])


class ComponentSummary(BaseModel):
    key: str
    """Registry key — a bare 'name@version' for public components, or
    'tenant/<tenant_id>/name@version' for a tenant-private one (doc §3.6)."""
    name: str
    version: str
    category: str
    displayName: str | None = None
    description: str | None = None


def _summarize(key: str, component: Component) -> ComponentSummary:
    return ComponentSummary(
        key=key,
        name=component.metadata.name,
        version=component.metadata.version,
        category=component.metadata.category,
        displayName=component.metadata.displayName,
        description=component.metadata.description,
    )


class ComponentVersionsResponse(BaseModel):
    name: str
    versions: list[ComponentSummary]
    json_schema: dict[str, Any]


@router.get("", response_model=list[ComponentSummary])
async def list_components(
    category: str | None = Query(default=None),
    principal: Principal = Depends(get_current_principal),
    service: RegistryService = Depends(get_registry_service),
) -> list[ComponentSummary]:
    """Entitlement-filtered registry listing (doc §3.6): admins see everything, anyone
    else sees only their entitled subset plus their own private components."""
    items = await service.list_components(principal, category=category)
    return [_summarize(key, c) for key, c in items]


@router.get("/{name}", response_model=ComponentVersionsResponse)
async def get_component_versions(
    name: str,
    principal: Principal = Depends(get_current_principal),
    service: RegistryService = Depends(get_registry_service),
) -> ComponentVersionsResponse:
    """All versions of one component name visible to the caller, plus the Component
    JSON Schema itself (so a manifest author can validate client-side before POSTing)."""
    versions = await service.get_component_versions(name, principal)
    return ComponentVersionsResponse(
        name=name,
        versions=[_summarize(key, c) for key, c in versions],
        json_schema=COMPONENT_SCHEMA,
    )


@router.post("", response_model=ComponentSummary, status_code=201)
async def register_component(
    body: dict[str, Any],
    principal: Principal = Depends(get_current_principal),
    service: RegistryService = Depends(get_registry_service),
) -> ComponentSummary:
    """Admins publish to the public catalog; anyone else needs a matching
    publish_grant and lands in their own tenant-private, namespaced catalog instead
    (doc §3.6) — never the other way around."""
    key, component = await service.register_component(body, principal)
    return _summarize(key, component)
