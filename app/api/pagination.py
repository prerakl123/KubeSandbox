"""Shared pagination for the growth-unbounded collections (Phase 9, UI integration).

An endpoint returning a bare, unbounded JSON array is a UI table that works in demos
and stops loading in production. Every collection whose row count grows with *use* —
sandboxes, runs, builds, usage records, credit requests, API keys — is paginated here.

Registry listings (`/v1/components`, `/v1/templates`) deliberately stay bare arrays:
they're bounded by what's committed under `components/`/`templates/` in git (doc §3.5),
a human-scale number that doesn't grow with traffic, and changing their response shape
now would break the SDK and every existing test for no benefit.

`limit`/`offset` rather than cursors. Honest about the tradeoff: offset pagination can
skip or repeat a row if the underlying set changes between pages, and gets slow at deep
offsets. Both are acceptable here — these are operator/user-facing views over
tenant-scoped data (tens to thousands of rows), always newest-first, and a UI paging
through its own sandbox list is not scanning a million-row table. A cursor API would be
the right answer if that stops being true.
"""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

from fastapi import Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
"""Capped so a client can't ask for the whole table in one request — the cap is the
actual protection here, since `limit` is caller-supplied."""


class PageParams(BaseModel):
    limit: int = DEFAULT_LIMIT
    offset: int = 0


async def page_params(
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_LIMIT, description=f"Rows per page (1-{MAX_LIMIT})."),
    ] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, description="Rows to skip.")] = 0,
) -> PageParams:
    """FastAPI dependency so every paginated route declares pagination identically —
    and so the bounds are enforced by FastAPI's own validation (a 422 with a clear
    message) rather than by each route remembering to clamp."""
    return PageParams(limit=limit, offset=offset)


PageParamsDep = Annotated[PageParams, Depends(page_params)]


class Page(BaseModel, Generic[T]):
    """Envelope for a paginated response.

    `total` is the count of matching rows *ignoring* limit/offset — without it a UI
    can't render "showing 1-50 of 812" or size a pager, which is the whole reason this
    is an envelope rather than a bare array plus headers. It costs one extra COUNT
    query per request; at these table sizes that's the right trade for a UI that can
    actually paginate.
    """

    items: list[T]
    total: int = Field(description="Total matching rows, ignoring limit/offset.")
    limit: int
    offset: int


async def paginate(
    session: AsyncSession, statement: Select, params: PageParams
) -> tuple[list, int]:
    """Run `statement` twice — once counted, once windowed — and return (rows, total).

    The count is derived from the caller's own statement with its ORDER BY stripped, so
    the filters can never drift apart between the two queries (the classic bug where
    the count reflects different criteria than the page). ORDER BY has to go because
    Postgres rejects ordering by a column that isn't in the subquery's select list, and
    it's meaningless inside a COUNT anyway.
    """
    count_statement = select(func.count()).select_from(statement.order_by(None).subquery())
    total = (await session.execute(count_statement)).scalar_one()
    rows = (
        (await session.execute(statement.limit(params.limit).offset(params.offset)))
        .scalars()
        .all()
    )
    return list(rows), int(total)
