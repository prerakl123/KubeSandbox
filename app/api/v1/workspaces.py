"""`GET /v1/workspaces/me` — the caller's persistent workspace (doc §10.2).

Not in doc §17's illustrative API surface, and unnecessary before Phase 9 because
nothing read a `Workspace` row back: the reconciler managed retention and
`create_sandbox(persistent=True)` mounted it, both server-side. A UI has to be able to
show a user how much of their 10 GiB they've used and whether retention has archived it,
or the whole persistent-workspace feature is invisible until it silently stops working.

Read-only on purpose. Creating a workspace is a side effect of the first persistent
sandbox (`WorkspaceService.get_or_create`), archiving/purging belongs to the reconciler's
retention sweep, and restoring is a deliberate, potentially slow operation
(`WorkspaceService.restore()`) that shouldn't be one accidental button press away — none
of those become better for having a client-facing verb.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_current_principal
from app.core.config import get_settings
from app.persistence.db import get_session
from app.persistence.models import Workspace

router = APIRouter(prefix="/v1/workspaces", tags=["Workspaces"])


class WorkspaceResponse(BaseModel):
    id: str
    state: str = Field(
        description="active | archived | deleted (doc §10.2). An `archived` workspace's "
        "volume is in cold storage and must be restored before a persistent sandbox can "
        "mount it — creating one against it fails loudly rather than silently mounting "
        "an empty volume."
    )
    quota_mb: int
    used_mb: int = Field(
        description="Last measured usage. Measured by the reconciler's retention sweep, "
        "not live — it needs a pod to read the volume, which is why it skips workspaces "
        "with a live sandbox attached. 0 means 'never measured', not necessarily empty."
    )
    used_percent: float = Field(description="Convenience for a UI meter; 0 when quota is 0.")
    last_access_at: datetime
    created_at: datetime


class WorkspaceStatusResponse(BaseModel):
    """Wrapped rather than returning the workspace (or a 404) directly.

    "Persistence is off in this deployment", "you have no workspace yet", and "here is
    your workspace" are three different states a UI must render differently, and
    collapsing the first two into a 404 would have it show "not found" for a feature
    that was simply never enabled.
    """

    enabled: bool = Field(description="Whether persistent workspaces are enabled here at all.")
    workspace: WorkspaceResponse | None = Field(
        description="Null when the caller has no workspace yet — it's created lazily by "
        "the first `POST /v1/sandboxes` with `persistent: true`."
    )
    retention: dict = Field(
        description="This deployment's configured doc §10.2 retention windows, so a UI "
        "can explain when an idle workspace will be archived and purged rather than "
        "hardcoding the defaults."
    )


def _summarize(row: Workspace) -> WorkspaceResponse:
    return WorkspaceResponse(
        id=row.id,
        state=row.state,
        quota_mb=row.quota_mb,
        used_mb=row.used_mb,
        used_percent=round(row.used_mb / row.quota_mb * 100, 2) if row.quota_mb else 0.0,
        last_access_at=row.last_access_at,
        created_at=row.created_at,
    )


@router.get(
    "/me",
    response_model=WorkspaceStatusResponse,
    summary="The caller's persistent workspace",
    description=(
        "Quota, usage, state, and this deployment's retention windows (doc §10.2). "
        "Returns `enabled: false` when persistence is off in this environment, and "
        "`workspace: null` when the caller simply hasn't created one yet — both are "
        "normal states, not errors."
    ),
    responses={400: {"description": "The caller is a service account, which has no user-scoped workspace."}},
)
async def my_workspace(
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceStatusResponse:
    settings = get_settings()
    retention = {
        "default_quota_mb": settings.workspace.default_quota_mb,
        "idle_retention_days": settings.workspace.idle_retention_days,
        "archive_grace_days": settings.workspace.archive_grace_days,
        "max_lifetime_days": settings.workspace.max_lifetime_days,
    }

    if principal.user_id is None:
        # A workspace is keyed on `user_id` (doc §10.2 — quota is per user), and an
        # API-key caller has no user. A 400 with a real reason beats returning an empty
        # response that looks like "you have no workspace".
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "a persistent workspace is per-user; a service-account caller has none",
        )

    row = (
        await session.execute(select(Workspace).where(Workspace.user_id == principal.user_id))
    ).scalar_one_or_none()
    return WorkspaceStatusResponse(
        enabled=settings.workspace.persistence_enabled,
        workspace=_summarize(row) if row is not None else None,
        retention=retention,
    )
