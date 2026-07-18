from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_current_principal, get_sandbox_service
from app.domain.execution import BatchRunResult
from app.persistence.db import get_session
from app.services.sandbox_service import SandboxService

router = APIRouter(prefix="/v1", tags=["execute"])


class ExecuteRequest(BaseModel):
    language: str
    version: str | None = None
    code: str
    stdin: str = ""


@router.post("/execute", response_model=BatchRunResult)
async def execute(
    body: ExecuteRequest,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    service: SandboxService = Depends(get_sandbox_service),
) -> BatchRunResult:
    """One-shot ephemeral batch run (doc §5.1): stdin is entirely up front, the result
    is one bundled object (stdout/stderr/exit_code/variables) — no live streaming."""
    return await service.execute(
        language=body.language,
        version=body.version,
        code=body.code,
        stdin=body.stdin,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        session=session,
    )
