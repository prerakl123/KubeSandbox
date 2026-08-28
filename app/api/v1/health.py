from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

from app.core.config import get_settings
from app.core.logging import get_logger
from app.persistence.db import get_session_factory

logger = get_logger(__name__)

router = APIRouter(tags=["Health"])

_DEPENDENCY_PROBE_TIMEOUT_SECONDS = 3.0
"""Each dependency check gets its own budget. Kept well under a typical
`readinessProbe.timeoutSeconds` so an unreachable Postgres/Redis produces a clean
"not ready" body the operator can read, rather than the kubelet killing the probe
mid-connect and reporting only a timeout with no detail about which dependency failed."""


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict:
    """Process is up. Never checks dependencies (DB/Redis/provisioner) — that's
    `/readyz`'s job, and conflating them is how a brief database blip turns into every
    replica being killed and restarted simultaneously. A `kubelet`/orchestrator
    liveness probe should hit this one."""
    return {"status": "ok"}


async def _check_database() -> None:
    async with get_session_factory()() as session:
        await session.execute(text("SELECT 1"))


async def _check_redis(request: Request) -> None:
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        # Only reachable when the lifespan hasn't run (e.g. a TestClient constructed
        # without entering the context manager) — reported as a real failure rather
        # than silently "ok", since a request-serving process with no Redis client
        # genuinely can't serve an attach.
        raise RuntimeError("redis client not initialized")
    await redis_client.ping()


@router.get(
    "/readyz",
    summary="Readiness probe",
    description=(
        "Whether this replica can actually serve traffic: Postgres and Redis are both "
        "reachable. Returns 503 with a per-dependency breakdown when one isn't, so a "
        "rolling update (and the HPA/PDB around it, doc §15) never sends traffic to a "
        "replica that would fail every request."
    ),
    responses={503: {"description": "One or more dependencies are unreachable."}},
)
async def readyz(request: Request, response: Response) -> dict:
    settings = get_settings()
    checks: dict[str, str] = {}

    # Probed concurrently, not sequentially: two dependencies each allowed 3s would
    # otherwise stack into a 6s worst case and blow through the probe's own timeout.
    results = await asyncio.gather(
        asyncio.wait_for(_check_database(), _DEPENDENCY_PROBE_TIMEOUT_SECONDS),
        asyncio.wait_for(_check_redis(request), _DEPENDENCY_PROBE_TIMEOUT_SECONDS),
        return_exceptions=True,
    )
    for name, result in zip(("database", "redis"), results, strict=True):
        if isinstance(result, BaseException):
            checks[name] = f"error: {result.__class__.__name__}: {result}"
        else:
            checks[name] = "ok"

    ready = all(value == "ok" for value in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning("readyz_not_ready", checks=checks)
    return {"status": "ok" if ready else "not_ready", "app_env": settings.app_env, "checks": checks}
