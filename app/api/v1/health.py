from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(tags=["Health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict:
    """Process is up. Never checks dependencies (DB/Redis/provisioner) — that's
    `/readyz`'s job. A `kubelet`/orchestrator liveness probe should hit this one."""
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe")
async def readyz() -> dict:
    """Dependencies the API needs are reachable.

    Kept dependency-free at import time; real DB/Redis pings are wired in once
    app/persistence/db.py exists (Phase 0 follow-up) so this can run standalone today.
    """
    settings = get_settings()
    return {"status": "ok", "app_env": settings.app_env}
