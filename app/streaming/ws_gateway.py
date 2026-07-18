"""WS /v1/sandboxes/{id}/attach — interactive PTY attach (doc §5.2, roadmap Phase 4).

**Single-viewer enforcement + reattach grace window** both ride on one Redis key,
`attach:{sandbox_id}`, holding the attached principal's identity with a
heartbeat-refreshed TTL. A second, *different* identity trying to attach while that
key is live is rejected with a real HTTP 409 during the WS handshake — raising
HTTPException before `websocket.accept()` makes FastAPI send a genuine HTTP error
response, not a WS close code, matching the doc's literal "409" wording. Reattach
falls out of the same mechanism for free: on disconnect the key is simply left to
expire naturally (no separate grace-window bookkeeping) — the *same* identity
reconnecting before it lapses is let straight back in (identity matches -> no
contention), a *different* identity is blocked until it lapses.
"""

from __future__ import annotations

import asyncio
import contextlib

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_redis_ws, get_sandbox_service_ws, get_session, get_ws_principal
from app.core.errors import KubeSandboxError, SandboxNotFoundError
from app.core.logging import get_logger
from app.provisioners.base import PTYStream
from app.services.sandbox_service import SandboxService
from app.streaming.pty_protocol import ExitFrame, OutputFrame, parse_client_frame

logger = get_logger(__name__)

router = APIRouter(tags=["Streaming"])

_LOCK_TTL_SECONDS = 30
"""Also doubles as the reattach grace window (see module docstring): how long a
disconnected viewer's claim survives before a *different* viewer may attach."""
_HEARTBEAT_INTERVAL_SECONDS = 10


def _lock_key(sandbox_id: str) -> str:
    return f"attach:{sandbox_id}"


def _viewer_identity(principal: Principal) -> str:
    return principal.user_id or f"service:{principal.tenant_id}"


async def _acquire_viewer_lock(r: redis.Redis, sandbox_id: str, identity: str) -> None:
    key = _lock_key(sandbox_id)
    # NX would reject our own reattach outright, so check-then-conditionally-set
    # instead of a plain SETNX: the *same* identity may always (re)claim the lock; a
    # *different* one only once the previous holder's key has actually expired.
    current = await r.get(key)
    if current is not None and current != identity:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "sandbox already has an active interactive viewer"
        )
    await r.set(key, identity, ex=_LOCK_TTL_SECONDS)


async def _heartbeat(r: redis.Redis, sandbox_id: str, identity: str) -> None:
    key = _lock_key(sandbox_id)
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
        # Only refresh if we still actually hold it — a stale heartbeat from an old
        # session must never resurrect a lock a different viewer has since taken.
        current = await r.get(key)
        if current == identity:
            await r.expire(key, _LOCK_TTL_SECONDS)


@router.websocket("/v1/sandboxes/{sandbox_id}/attach")
async def attach(
    websocket: WebSocket,
    sandbox_id: str,
    principal: Principal = Depends(get_ws_principal),
    session: AsyncSession = Depends(get_session),
    service: SandboxService = Depends(get_sandbox_service_ws),
    r: redis.Redis = Depends(get_redis_ws),
) -> None:
    try:
        row = await service.get_sandbox(sandbox_id, principal.tenant_id, session)
    except SandboxNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such sandbox") from exc
    if row.state == "terminated":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such sandbox")

    identity = _viewer_identity(principal)
    await _acquire_viewer_lock(r, sandbox_id, identity)

    await websocket.accept()

    try:
        pty = await service.open_pty(sandbox_id, principal.tenant_id, session)
    except (SandboxNotFoundError, KubeSandboxError) as exc:
        logger.warning("attach_open_pty_failed", sandbox_id=sandbox_id, error=str(exc))
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    heartbeat_task = asyncio.create_task(_heartbeat(r, sandbox_id, identity))
    try:
        await _pump(websocket, pty)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await pty.close()
        # Deliberately NOT deleting the lock key here — letting it expire naturally is
        # what implements the reattach grace window (see module docstring).


async def _pty_to_ws(websocket: WebSocket, pty: PTYStream) -> None:
    while True:
        event = await pty.read()
        if event is None:
            return
        if event.kind == "output":
            await websocket.send_json(OutputFrame.from_bytes(event.data or b"").model_dump())
        elif event.kind == "exit":
            await websocket.send_json(ExitFrame(exit_code=event.exit_code or 0).model_dump())
            return


async def _ws_to_pty(websocket: WebSocket, pty: PTYStream) -> None:
    while True:
        raw = await websocket.receive_text()
        try:
            frame = parse_client_frame(raw)
        except ValueError as exc:
            logger.warning("attach_bad_client_frame", error=str(exc))
            continue
        if frame.type == "stdin":
            await pty.write_stdin(frame.decode())
        elif frame.type == "resize":
            await pty.resize(cols=frame.cols, rows=frame.rows)
        elif frame.type == "signal":
            await pty.write_stdin(frame.to_control_bytes())


async def _pump(websocket: WebSocket, pty: PTYStream) -> None:
    pty_task = asyncio.create_task(_pty_to_ws(websocket, pty))
    ws_task = asyncio.create_task(_ws_to_pty(websocket, pty))
    try:
        done, _pending = await asyncio.wait({pty_task, ws_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    finally:
        for task in (pty_task, ws_task):
            task.cancel()
        await asyncio.gather(pty_task, ws_task, return_exceptions=True)
