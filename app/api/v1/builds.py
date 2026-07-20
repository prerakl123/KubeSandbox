from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_build_manager, get_current_principal, get_registry
from app.core.errors import ComponentNotFoundError
from app.extensions.loader import Registry
from app.persistence.db import get_session
from app.persistence.models import Build
from app.services.build_manager import BuildManager

router = APIRouter(prefix="/v1", tags=["Builds"])


class BuildResponse(BaseModel):
    id: str = Field(description="Build id — pass this to GET /v1/builds/{id} to poll.")
    component_name: str
    component_version: str
    strategy: str = Field(description="dockerfile | compose | pipeline | helm")
    status: str = Field(description="pending | running | succeeded | failed")
    image_ref: str | None = Field(description="Resolved, pullable ref once a successful image build is pushed.")
    artifact_ref: str | None = Field(description="Object-store key for a successful manifest artifact (helm).")
    error: str | None
    log_excerpt: str | None = Field(description="Tail of the build log, truncated.")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _summarize(row: Build) -> BuildResponse:
    return BuildResponse(
        id=row.id,
        component_name=row.component_name,
        component_version=row.component_version,
        strategy=row.strategy,
        status=row.status,
        image_ref=row.image_ref,
        artifact_ref=row.artifact_ref,
        error=row.error,
        log_excerpt=row.log_excerpt,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _resolve_build_target(registry: Registry, name: str, version: str | None) -> str:
    """`name` may be bare (public) or tenant-qualified ("tenant/<id>/<name>") — mirrors
    RegistryService.get_component_versions' own name-matching convention."""
    if version is not None:
        key = f"{name}@{version}"
    else:
        component = registry.latest_component(name)  # raises ComponentNotFoundError
        key = f"{name}@{component.metadata.version}"
    if key not in registry.components:
        raise ComponentNotFoundError(key)
    return key


@router.post(
    "/components/{name}/build",
    response_model=BuildResponse,
    status_code=202,
    summary="Trigger a build",
    description=(
        "Runs the component's declared build strategy (doc §8 — dockerfile/compose/"
        "pipeline/helm) to produce a real, pushed golden image (or, for helm, a "
        "rendered manifest artifact). Runs in the background; poll "
        "`GET /v1/builds/{id}`. Admin can build any public component; a non-admin "
        "only their own tenant-private one — the same trust boundary as publishing it."
    ),
)
async def trigger_build(
    background_tasks: BackgroundTasks,
    name: str = Path(description="Bare or tenant-qualified component name."),
    version: str | None = Query(default=None, description="Exact version; omit for the latest."),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    registry: Registry = Depends(get_registry),
    build_manager: BuildManager = Depends(get_build_manager),
) -> BuildResponse:
    component_key = _resolve_build_target(registry, name, version)
    build_row, is_new = await build_manager.trigger_build(component_key, principal, session)
    if is_new:
        background_tasks.add_task(build_manager.run_build, build_row.id)
    return _summarize(build_row)


@router.get(
    "/builds/{id}",
    response_model=BuildResponse,
    summary="Get build status",
    description="Poll target for POST /v1/components/{name}/build. Admin sees any "
    "build; a non-admin only their own tenant's.",
)
async def get_build(
    id: str = Path(description="Build id, as returned by POST /v1/components/{name}/build."),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    build_manager: BuildManager = Depends(get_build_manager),
) -> BuildResponse:
    row = await build_manager.get_build(id, principal, session)
    return _summarize(row)
