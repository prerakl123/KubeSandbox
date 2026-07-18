from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import Principal, get_current_principal, get_template_service
from app.domain.manifests import SandboxTemplate
from app.services.template_service import TemplateService

router = APIRouter(prefix="/v1/templates", tags=["templates"])


class TemplateSummary(BaseModel):
    key: str
    """Registry key — a bare 'name@version' for a public template, or
    'tenant/<tenant_id>/name@version' for a tenant-private one (doc §3.6)."""
    name: str
    version: str
    displayName: str | None = None
    description: str | None = None
    base_ref: str
    component_refs: list[str]


def _summarize(key: str, template: SandboxTemplate) -> TemplateSummary:
    return TemplateSummary(
        key=key,
        name=template.metadata.name,
        version=template.metadata.version,
        displayName=template.metadata.displayName,
        description=template.metadata.description,
        base_ref=template.spec.base.ref,
        component_refs=[c.ref for c in template.spec.components],
    )


@router.get("", response_model=list[TemplateSummary])
async def list_templates(
    name: str | None = Query(default=None),
    principal: Principal = Depends(get_current_principal),
    service: TemplateService = Depends(get_template_service),
) -> list[TemplateSummary]:
    """A public template is visible only if every component it references is itself
    visible to the caller (doc §3.6 — there's no separate template_entitlements
    table); admins always see everything."""
    items = await service.list_templates(principal, name=name)
    return [_summarize(key, t) for key, t in items]


@router.post("", response_model=TemplateSummary, status_code=201)
async def create_template(
    body: dict[str, Any],
    principal: Principal = Depends(get_current_principal),
    service: TemplateService = Depends(get_template_service),
) -> TemplateSummary:
    """Admins publish to the public catalog; anyone else needs a 'template'
    publish_grant and lands in their own tenant-private, namespaced catalog instead."""
    key, template = await service.create_template(body, principal)
    return _summarize(key, template)
