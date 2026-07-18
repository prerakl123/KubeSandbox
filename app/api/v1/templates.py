from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import Principal, get_current_principal, get_template_service
from app.domain.manifests import SandboxTemplate
from app.services.template_service import TemplateService

router = APIRouter(prefix="/v1/templates", tags=["Templates"])


class TemplateSummary(BaseModel):
    key: str = Field(
        description="Registry key — a bare 'name@version' for a public template, or "
        "'tenant/<tenant_id>/name@version' for a tenant-private one (doc §3.6)."
    )
    name: str
    version: str
    displayName: str | None = None
    description: str | None = None
    base_ref: str = Field(description="Component ref this template's base image points to.")
    component_refs: list[str] = Field(description="Component refs composed on top of the base.")


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


@router.get(
    "",
    response_model=list[TemplateSummary],
    summary="List templates",
    description="A public template is visible only if every component it references "
    "is itself visible to the caller (doc §3.6); admins always see everything.",
)
async def list_templates(
    name: str | None = Query(default=None, description="Filter to templates with this name."),
    principal: Principal = Depends(get_current_principal),
    service: TemplateService = Depends(get_template_service),
) -> list[TemplateSummary]:
    items = await service.list_templates(principal, name=name)
    return [_summarize(key, t) for key, t in items]


@router.post(
    "",
    response_model=TemplateSummary,
    status_code=201,
    summary="Register a SandboxTemplate manifest",
    description=(
        "Admins publish to the public catalog; anyone else needs a matching "
        "'template' `publish_grant` and lands in their own tenant-private catalog "
        "instead (doc §3.6). Body is a raw SandboxTemplate manifest."
    ),
)
async def create_template(
    body: dict[str, Any],
    principal: Principal = Depends(get_current_principal),
    service: TemplateService = Depends(get_template_service),
) -> TemplateSummary:
    key, template = await service.create_template(body, principal)
    return _summarize(key, template)
