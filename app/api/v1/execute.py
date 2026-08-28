from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_current_principal, get_sandbox_service
from app.domain.execution import BatchRunResult
from app.persistence.db import get_session
from app.services.sandbox_service import SandboxService

router = APIRouter(prefix="/v1", tags=["Execution"])


class ExecuteRequest(BaseModel):
    language: str = Field(
        description="Language/tool component id to run (e.g. 'python', 'node', 'go', "
        "'bash'). When `template` is set, picks which of the template's mainTool "
        "components to run instead of resolving an ad-hoc single component."
    )
    version: str | None = Field(
        default=None,
        description="Exact component version to pin (e.g. '3.12.4'); omit for the "
        "latest published version. Mutually exclusive with `template` — pin a "
        "template's component versions inside the template manifest itself.",
    )
    template: str | None = Field(
        default=None,
        description="SandboxTemplate ref ('name@version') to compose the sandbox "
        "from (doc §3.4), instead of a single ad-hoc component.",
    )
    code: str = Field(description="Source code to run, written to the language's default filename.")
    stdin: str = Field(
        default="",
        description="Entirely fed to the process up front, then closed (EOF) — there "
        "is no live stdin wait on this path (doc §5.1).",
    )

    @model_validator(mode="after")
    def _version_only_without_template(self) -> "ExecuteRequest":
        if self.template and self.version:
            raise ValueError("'version' pins an ad-hoc component version; it has no "
                              "meaning alongside 'template' — pin the component's "
                              "version inside the template's own component refs instead")
        return self


class AsyncRunAccepted(BaseModel):
    """Returned by `?async=true` instead of a result (doc §5.1)."""

    run_id: str = Field(description="Poll `GET /v1/runs/{run_id}` with this.")
    status: str = Field(description="Always 'pending' here; it moves to running, then completed/failed.")


@router.post(
    "/execute",
    response_model=BatchRunResult | AsyncRunAccepted,
    summary="Run one-shot ephemeral batch code",
    description=(
        "Claims/creates an ephemeral sandbox, runs the given code to completion (or "
        "timeout), tears it down, and returns one bundled result — no live streaming, "
        "no runtime stdin wait (doc §5.1). The workflow-builder 'code block' "
        "convenience path; for a longer-lived sandbox see `POST /v1/sandboxes`.\n\n"
        "With `?async=true` the call returns `202` and a `run_id` immediately; poll "
        "`GET /v1/runs/{run_id}` until `status` is `completed` or `failed`, at which "
        "point that endpoint returns the same bundled result body. Still no incremental "
        "output on either variant — only 'done vs. not done yet'."
    ),
    responses={
        200: {"description": "Synchronous run finished; body is the bundled result."},
        202: {"description": "Async run accepted; body carries the run_id to poll."},
    },
)
async def execute(
    body: ExecuteRequest,
    background_tasks: BackgroundTasks,
    response: Response,
    run_async: Annotated[
        bool,
        Query(
            alias="async",
            description="Return a run_id immediately instead of blocking until the run "
            "finishes (doc §5.1). Useful for a UI that wants to render progress, or for "
            "a caller whose own HTTP timeout is tighter than the sandbox's wall-clock cap.",
        ),
    ] = False,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    service: SandboxService = Depends(get_sandbox_service),
) -> BatchRunResult | AsyncRunAccepted:
    if run_async:
        # The spec is resolved (and a bad language/template rejected) inside
        # start_async_run, before anything is scheduled — so a typo still fails *this*
        # request with a real 404 rather than returning 202 and failing invisibly later.
        run_row = await service.start_async_run(
            language=body.language,
            version=body.version,
            template=body.template,
            tenant_id=principal.tenant_id,
            session=session,
        )
        background_tasks.add_task(
            service.run_async,
            run_row.id,
            language=body.language,
            code=body.code,
            version=body.version,
            stdin=body.stdin,
            template=body.template,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return AsyncRunAccepted(run_id=run_row.id, status=run_row.status)

    return await service.execute(
        language=body.language,
        version=body.version,
        template=body.template,
        code=body.code,
        stdin=body.stdin,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        session=session,
    )
