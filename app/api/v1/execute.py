from __future__ import annotations

from fastapi import APIRouter, Depends
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


@router.post(
    "/execute",
    response_model=BatchRunResult,
    summary="Run one-shot ephemeral batch code",
    description=(
        "Claims/creates an ephemeral sandbox, runs the given code to completion (or "
        "timeout), tears it down, and returns one bundled result — no live streaming, "
        "no runtime stdin wait (doc §5.1). The workflow-builder 'code block' "
        "convenience path; for a longer-lived sandbox see `POST /v1/sandboxes`."
    ),
)
async def execute(
    body: ExecuteRequest,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    service: SandboxService = Depends(get_sandbox_service),
) -> BatchRunResult:
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
