from __future__ import annotations

import posixpath
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_current_principal, get_sandbox_service
from app.domain.execution import BatchRunResult
from app.persistence.db import get_session
from app.persistence.models import Sandbox
from app.services.sandbox_service import SandboxService

router = APIRouter(prefix="/v1/sandboxes", tags=["Sandboxes"])

_WORKSPACE_ROOT = "/workspace"

_SANDBOX_ID = Path(description="Sandbox id, as returned by `POST /v1/sandboxes`.")


def _validate_relative_path(path: str, *, allow_root: bool) -> str:
    """Bounds a client-supplied path to the sandbox workspace (doc §5.4) — rejects
    anything absolute or that `..`s its way out, before any provisioner call ever sees
    it. Returns the path relative to `/workspace` (possibly empty when `allow_root`)."""
    if path in ("", ".", "/"):
        if allow_root:
            return ""
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path must not be empty")
    if posixpath.isabs(path):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path must be relative to the sandbox workspace")
    normalized = posixpath.normpath(path)
    if normalized == ".." or normalized.startswith("../"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path must not escape the sandbox workspace")
    return normalized


class CreateSandboxRequest(BaseModel):
    language: str = Field(
        description="Language/tool component id to run later (e.g. 'python', 'node', "
        "'go', 'bash'). When `template` is set, picks which of the template's "
        "mainTool components later calls (runs, attach) act against."
    )
    version: str | None = Field(
        default=None,
        description="Exact component version to pin; omit for the latest published "
        "version. Mutually exclusive with `template`.",
    )
    template: str | None = Field(
        default=None,
        description="SandboxTemplate ref ('name@version') to compose the sandbox "
        "from (doc §3.4), instead of a single ad-hoc component.",
    )
    persistent: bool = Field(
        default=False,
        description="Mount the caller's durable per-user workspace (doc §10.2) at "
        "/workspace instead of ephemeral storage — created lazily on first use, "
        "subject to that user's quota, and never returned to a warm pool. Requires "
        "an authenticated user and `workspace.persistence_enabled` in this environment.",
    )


class SandboxResponse(BaseModel):
    id: str = Field(description="Sandbox id — pass this to every other /v1/sandboxes/{id} call.")
    state: str = Field(
        description="pending | provisioning | ready | active | idle | terminating | "
        "terminated | failed (doc §4.1's state machine)."
    )
    backend: str = Field(description="Which Provisioner realized this sandbox: 'docker' or 'kubernetes'.")
    template_ref: str | None = Field(description="SandboxTemplate ref this sandbox was composed from, if any.")
    component_refs: list[str] = Field(description="Resolved component keys ('name@version') backing this sandbox.")
    weight_class: str = Field(description="light | standard | heavy — drives pooling/segregation (doc §4.3).")
    persistent: bool = Field(description="Whether this sandbox mounted a durable per-user workspace (doc §10.2).")
    created_at: datetime
    last_active_at: datetime | None = Field(description="Last time a batch run or attach touched this sandbox.")
    terminated_at: datetime | None = Field(description="When this sandbox was torn down, if it has been.")


def _summarize(row: Sandbox, *, state_override: str | None = None) -> SandboxResponse:
    return SandboxResponse(
        id=row.id,
        state=state_override or row.state,
        backend=row.backend,
        template_ref=row.template_ref,
        component_refs=list(row.component_refs or []),
        weight_class=row.weight_class,
        persistent=row.persistent,
        created_at=row.created_at,
        last_active_at=row.last_active_at,
        terminated_at=row.terminated_at,
    )


class RunRequest(BaseModel):
    language: str | None = Field(
        default=None,
        description="Only needed when the sandbox was created from a template with "
        "more than one runnable component; an ad-hoc single-component sandbox never "
        "needs this.",
    )
    code: str = Field(description="Source code to run, written to the language's default filename.")
    stdin: str = Field(
        default="",
        description="Entirely fed to the process up front, then closed (EOF) — no live stdin wait (doc §5.1).",
    )


@router.post(
    "",
    response_model=SandboxResponse,
    status_code=201,
    summary="Create a long-lived sandbox",
    description=(
        "Acquires a sandbox that outlives this request (doc §17) — unlike "
        "`POST /v1/execute`, the caller tears it down explicitly via `DELETE`, runs "
        "batch commands against it via `POST .../runs`, or attaches an interactive "
        "PTY session via `WS .../attach`."
    ),
)
async def create_sandbox(
    body: CreateSandboxRequest,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    service: SandboxService = Depends(get_sandbox_service),
) -> SandboxResponse:
    row = await service.create_sandbox(
        language=body.language,
        version=body.version,
        template=body.template,
        persistent=body.persistent,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        session=session,
    )
    return _summarize(row)


@router.get(
    "/{sandbox_id}",
    response_model=SandboxResponse,
    summary="Get sandbox status",
    description="Live status (doc §17) — asks the provisioner, not just the last-known "
    "DB row, and self-heals the row if the sandbox was reaped out-of-band.",
)
async def get_sandbox(
    sandbox_id: str = _SANDBOX_ID,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    service: SandboxService = Depends(get_sandbox_service),
) -> SandboxResponse:
    row, live_status = await service.get_sandbox_status(sandbox_id, principal.tenant_id, session)
    return _summarize(row, state_override=live_status.state.value)


@router.delete(
    "/{sandbox_id}",
    status_code=204,
    summary="Destroy a sandbox",
    description="Graceful eradication (doc §4.1) — idempotent, safe to call more than once.",
)
async def destroy_sandbox(
    sandbox_id: str = _SANDBOX_ID,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    service: SandboxService = Depends(get_sandbox_service),
) -> None:
    await service.destroy_sandbox(sandbox_id, principal.tenant_id, session)


@router.post(
    "/{sandbox_id}/runs",
    response_model=BatchRunResult,
    summary="Run a batch command in an existing sandbox",
    description=(
        "Same bundled batch contract as `POST /v1/execute` (doc §5.1), against an "
        "existing warm sandbox instead of a fresh ephemeral one. Never destroys the "
        "sandbox, win or lose."
    ),
)
async def run_in_sandbox(
    body: RunRequest,
    sandbox_id: str = _SANDBOX_ID,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    service: SandboxService = Depends(get_sandbox_service),
) -> BatchRunResult:
    return await service.run_in_sandbox(
        sandbox_id,
        code=body.code,
        stdin=body.stdin,
        language=body.language,
        tenant_id=principal.tenant_id,
        session=session,
    )


class FileEntryResponse(BaseModel):
    path: str = Field(description="Path relative to the listing root.")
    is_dir: bool = Field(description="True for a directory, false for a regular file.")


_FILE_PATH_QUERY = Query(description="Path relative to the sandbox's /workspace (e.g. 'src/main.py'). Must not escape it.")


@router.get(
    "/{sandbox_id}/files",
    summary="Download a workspace file",
    description="Reads one file out of the sandbox's /workspace (doc §5.4). Returned as raw bytes.",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_file(
    sandbox_id: str = _SANDBOX_ID,
    path: str = _FILE_PATH_QUERY,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    service: SandboxService = Depends(get_sandbox_service),
) -> Response:
    relative = _validate_relative_path(path, allow_root=False)
    absolute = posixpath.join(_WORKSPACE_ROOT, relative)
    content = await service.get_file(sandbox_id, principal.tenant_id, absolute, session=session)
    return Response(content=content, media_type="application/octet-stream")


@router.put(
    "/{sandbox_id}/files",
    status_code=204,
    summary="Upload a workspace file",
    description=(
        "Writes one file into the sandbox's /workspace (doc §5.4). Body is decoded "
        "as UTF-8 text — matching `put_files()`'s existing str-content contract (the "
        "same one batch execution's `files` argument uses); a non-UTF-8 upload is "
        "rejected with a clear 400 rather than silently corrupted."
    ),
)
async def upload_file(
    path: str = _FILE_PATH_QUERY,
    body: bytes = Body(description="Raw file content, must be valid UTF-8 text."),
    sandbox_id: str = _SANDBOX_ID,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    service: SandboxService = Depends(get_sandbox_service),
) -> None:
    relative = _validate_relative_path(path, allow_root=False)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"file content must be UTF-8: {exc}") from exc
    await service.put_file(sandbox_id, principal.tenant_id, relative, text, session=session)


@router.get(
    "/{sandbox_id}/tree",
    response_model=list[FileEntryResponse],
    summary="List workspace files",
    description="Recursive file listing under `path` (doc §5.4), defaulting to the whole workspace.",
)
async def get_tree(
    sandbox_id: str = _SANDBOX_ID,
    path: str = Query(default="", description="Subdirectory to list, relative to /workspace. Empty means the whole workspace."),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    service: SandboxService = Depends(get_sandbox_service),
) -> list[FileEntryResponse]:
    relative = _validate_relative_path(path, allow_root=True)
    absolute = posixpath.join(_WORKSPACE_ROOT, relative) if relative else _WORKSPACE_ROOT
    entries = await service.list_tree(sandbox_id, principal.tenant_id, absolute, session=session)
    return [FileEntryResponse(path=e.path, is_dir=e.is_dir) for e in entries]
