from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness: process is up. Never checks dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict:
    """Readiness: dependencies the API needs are reachable.

    Kept dependency-free at import time; real DB/Redis pings are wired in once
    app/persistence/db.py exists (Phase 0 follow-up) so this can run standalone today.
    """
    settings = get_settings()
    return {"status": "ok", "app_env": settings.app_env}
