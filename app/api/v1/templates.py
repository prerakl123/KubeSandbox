from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import Principal, get_current_principal, get_template_service
from app.core.errors import TemplateNotFoundError
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


class TemplateDetail(TemplateSummary):
    """A template's full composition and the sandbox shape it produces.

    `GET /v1/templates` returns summaries; this is the detail view a UI's "choose an
    environment" screen needs — a user picking between templates is choosing CPU,
    memory, TTL, and whether their files persist, and none of that is in the summary.
    """

    weight_class: str | None = Field(
        description="light | standard | heavy (doc §4.3), or null when the template "
        "leaves it to be derived from its components' own weight classes."
    )
    cpu: str
    memory: str
    ephemeral_storage_mb: int | None
    persistent_workspace: bool = Field(
        description="Whether sandboxes from this template mount a durable workspace (doc §10.2)."
    )
    workspace_size_mb: int | None
    ttl_idle: str = Field(description="Idle TTL as a duration string, e.g. '15m' (doc §4.1).")
    ttl_max: str = Field(description="Absolute max lifetime, e.g. '2h'.")


class TemplateVersionsResponse(BaseModel):
    name: str
    versions: list[TemplateDetail]


def _detail(key: str, template: SandboxTemplate) -> TemplateDetail:
    spec = template.spec
    workspace = spec.workspace
    return TemplateDetail(
        **_summarize(key, template).model_dump(),
        weight_class=spec.weightClass.value if spec.weightClass is not None else None,
        cpu=spec.resources.cpu,
        memory=spec.resources.memory,
        ephemeral_storage_mb=spec.resources.ephemeralStorageMB,
        persistent_workspace=bool(workspace.persistent) if workspace is not None else False,
        workspace_size_mb=workspace.sizeMB if workspace is not None else None,
        ttl_idle=spec.ttl.idle,
        ttl_max=spec.ttl.max,
    )


@router.get(
    "/{name}",
    response_model=TemplateVersionsResponse,
    summary="Get a template's versions and full spec",
    description=(
        "Every version of one template name visible to the caller (entitlement-filtered, "
        "doc §3.6), with the composition and resource/TTL shape of each. The counterpart "
        "to `GET /v1/components/{name}`, which existed while this didn't."
    ),
    responses={404: {"description": "No such template, or the caller isn't entitled to it."}},
)
async def get_template_versions(
    name: str,
    principal: Principal = Depends(get_current_principal),
    service: TemplateService = Depends(get_template_service),
) -> TemplateVersionsResponse:
    items = await service.list_templates(principal, name=name)
    if not items:
        # Not-entitled and doesn't-exist are reported identically, the same rule the
        # component endpoint follows (doc §3.6) — otherwise a caller could enumerate
        # other tenants' private template names by distinguishing 403 from 404.
        raise TemplateNotFoundError(name)
    return TemplateVersionsResponse(name=name, versions=[_detail(key, t) for key, t in items])


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
