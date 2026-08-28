"""`GET /v1/runs` and `GET /v1/runs/{run_id}` — run history and the async poll target.

`GET /v1/runs/{run_id}` is listed in doc §17's API surface and is the polling half of
doc §5.1's `?async=true` contract ("poll `GET /v1/runs/{run_id}` until
`status=completed`, at which point the same bundled result body is returned"), but it
had never been implemented — other docs in this repo already referenced it as if it
existed. Phase 9 closes that, together with `POST /v1/execute?async=true` itself.

`GET /v1/runs` (the list) isn't in doc §17 at all. It's here because a UI's "recent
runs" view is the single most obvious screen in a code-sandbox product, and the data was
already being persisted with nothing able to read it back.

Both are tenant-scoped, never user-scoped: a run belongs to a tenant (`runs.tenant_id`),
and doc §11 makes the tenant the isolation boundary. A per-user filter is available via
`?sandbox_id=` for the narrower "this session's runs" view.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_current_principal
from app.api.pagination import Page, PageParamsDep, paginate
from app.persistence.db import get_session
from app.persistence.models import Run

router = APIRouter(prefix="/v1/runs", tags=["Runs"])

TERMINAL_STATUSES = ("completed", "failed")


class RunSummary(BaseModel):
    """A run without its output — what a history table renders. Excerpts are omitted
    deliberately: a list of 50 runs each carrying 10 KB of stdout is a slow endpoint
    nobody asked for. Fetch the detail view for output."""

    id: str
    sandbox_id: str | None
    status: str = Field(description="pending | running | completed | failed (doc §5.1).")
    component_ref: str | None = Field(description="Component that ran, as 'name@version'.")
    exit_code: int | None = Field(description="Null until the run reaches a terminal status.")
    duration_ms: int | None
    truncated: bool
    timed_out: bool
    created_at: datetime
    finished_at: datetime | None


class RunDetail(RunSummary):
    """The full record, including the bundled result. For a `completed` run this carries
    the same `stdout`/`stderr`/`exit_code`/`variables` a synchronous `POST /v1/execute`
    would have returned — that's the doc §5.1 promise that polling yields "the same
    bundled result body".

    The output fields are excerpts (the first 10 KB each), which is what `runs` stores;
    doc §10.1 puts full logs in the object store, and no endpoint serves those yet — see
    the Phase 9 scope boundaries in docs/TASK_CHECKLIST.md.
    """

    command: list[str]
    stdout: str = Field(description="First 10 KB of stdout, as persisted.")
    stderr: str = Field(description="First 10 KB of stderr, as persisted.")
    variables: dict[str, Any] | None = Field(description="Doc §5.3's variable dump, when supported.")
    error: str | None = Field(
        description="Why a `failed` run produced no result at all — a control-plane "
        "failure, distinct from the program's own stderr."
    )


def _summarize(row: Run) -> RunSummary:
    return RunSummary(
        id=row.id,
        sandbox_id=row.sandbox_id,
        status=row.status,
        component_ref=row.component_ref,
        exit_code=row.exit_code,
        duration_ms=row.duration_ms,
        truncated=row.truncated,
        timed_out=row.timed_out,
        created_at=row.created_at,
        finished_at=row.finished_at,
    )


def _detail(row: Run) -> RunDetail:
    return RunDetail(
        **_summarize(row).model_dump(),
        command=list(row.command or []),
        stdout=row.stdout_excerpt or "",
        stderr=row.stderr_excerpt or "",
        variables=row.variables,
        error=row.error,
    )


@router.get(
    "",
    response_model=Page[RunSummary],
    summary="List runs",
    description=(
        "This tenant's run history, newest first, without output bodies. Filter by "
        "`sandbox_id` for one session's runs, or by `status` to find what's still in "
        "flight."
    ),
)
async def list_runs(
    params: PageParamsDep,
    sandbox_id: Annotated[str | None, Query(description="Only runs against this sandbox.")] = None,
    run_status: Annotated[
        str | None,
        Query(alias="status", description="pending | running | completed | failed."),
    ] = None,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> Page[RunSummary]:
    statement = select(Run).where(Run.tenant_id == principal.tenant_id)
    if sandbox_id is not None:
        statement = statement.where(Run.sandbox_id == sandbox_id)
    if run_status is not None:
        statement = statement.where(Run.status == run_status)
    # `id` as a tiebreaker so pages are deterministic: several runs in one tick can
    # share a `created_at` to the database's timestamp resolution, and without a
    # tiebreaker the same row can appear on two pages or on neither.
    statement = statement.order_by(Run.created_at.desc(), Run.id.desc())
    rows, total = await paginate(session, statement, params)
    return Page[RunSummary](
        items=[_summarize(r) for r in rows], total=total, limit=params.limit, offset=params.offset
    )


@router.get(
    "/{run_id}",
    response_model=RunDetail,
    summary="Get a run's status and bundled result",
    description=(
        "Doc §17's poll target for `POST /v1/execute?async=true` (doc §5.1). Poll until "
        "`status` is `completed` or `failed`; a `completed` run carries the same bundled "
        "stdout/stderr/exit_code/variables the synchronous call would have returned. "
        "There is deliberately no incremental output on this path — only 'done vs. not "
        "done yet' (doc §5.1)."
    ),
    responses={404: {"description": "No such run in the caller's tenant."}},
)
async def get_run(
    run_id: str = Path(description="Run id, as returned by POST /v1/execute?async=true."),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> RunDetail:
    row = await session.get(Run, run_id)
    # Another tenant's run is reported as 404, not 403 — same rule as every other
    # tenant-scoped lookup here, so run ids can't be probed.
    if row is None or row.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such run: {run_id}")
    return _detail(row)
