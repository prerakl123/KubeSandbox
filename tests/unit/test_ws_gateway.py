"""Unit tests for the WS attach gateway's single-viewer lock/reattach logic and frame
pump, using a tiny in-memory fake standing in for the three Redis calls actually used
(get/set-with-ttl/expire) and fake WebSocket/PTYStream doubles — no real Redis, no
Starlette TestClient (which runs the ASGI app on a separate thread/event loop that
doesn't mix safely with an asyncio session created in the test's own loop, same
reasoning as tests/integration/test_execute_docker.py's docstring). Real WS/dtach/
resize behavior against a live daemon is live-verification territory, not mocked here.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json

import pytest
from fastapi import HTTPException, WebSocketDisconnect

from app.domain.auth import Principal
from app.provisioners.base import PTYEvent
from app.streaming import ws_gateway
from app.streaming.ws_gateway import _acquire_viewer_lock, _heartbeat, _pump, _viewer_identity


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    async def expire(self, key: str, seconds: int) -> None:
        self.expire_calls.append((key, seconds))


class FakeWebSocket:
    def __init__(self, incoming: list[str]) -> None:
        self._incoming = list(incoming)
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        if not self._incoming:
            raise WebSocketDisconnect()
        return self._incoming.pop(0)


class FakePTYStream:
    def __init__(self, events: list[PTYEvent]) -> None:
        self._events = list(events)
        self.written: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []

    async def write_stdin(self, data: bytes) -> None:
        self.written.append(data)

    async def resize(self, *, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))

    async def read(self) -> PTYEvent | None:
        if not self._events:
            return None
        return self._events.pop(0)

    async def close(self) -> None:
        pass


# -- viewer identity -------------------------------------------------------------------


def test_viewer_identity_prefers_user_id():
    principal = Principal(tenant_id="t1", user_id="u1", role="user")
    assert _viewer_identity(principal) == "u1"


def test_viewer_identity_falls_back_to_tenant_for_service_principal():
    principal = Principal(tenant_id="t1", user_id=None, role="service")
    assert _viewer_identity(principal) == "service:t1"


# -- single-viewer lock / reattach grace window -----------------------------------------


async def test_acquire_viewer_lock_allows_same_identity_reattach():
    r = FakeRedis()
    await _acquire_viewer_lock(r, "sb1", "user-a")
    await _acquire_viewer_lock(r, "sb1", "user-a")  # must not raise
    assert r.store["attach:sb1"] == "user-a"


async def test_acquire_viewer_lock_rejects_different_identity_while_held():
    r = FakeRedis()
    await _acquire_viewer_lock(r, "sb1", "user-a")

    with pytest.raises(HTTPException) as exc_info:
        await _acquire_viewer_lock(r, "sb1", "user-b")
    assert exc_info.value.status_code == 409


async def test_acquire_viewer_lock_allows_different_identity_after_natural_expiry():
    r = FakeRedis()
    await _acquire_viewer_lock(r, "sb1", "user-a")
    del r.store["attach:sb1"]  # simulate the TTL lapsing (the reattach grace window)

    await _acquire_viewer_lock(r, "sb1", "user-b")  # must not raise
    assert r.store["attach:sb1"] == "user-b"


async def test_heartbeat_refreshes_ttl_while_still_holding_the_lock(monkeypatch):
    monkeypatch.setattr(ws_gateway, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    r = FakeRedis()
    await _acquire_viewer_lock(r, "sb1", "user-a")

    task = asyncio.create_task(_heartbeat(r, "sb1", "user-a"))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(r.expire_calls) >= 1
    assert all(key == "attach:sb1" for key, _ in r.expire_calls)


async def test_heartbeat_never_resurrects_a_lock_a_different_viewer_took_over(monkeypatch):
    monkeypatch.setattr(ws_gateway, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    r = FakeRedis()
    await _acquire_viewer_lock(r, "sb1", "user-a")

    task = asyncio.create_task(_heartbeat(r, "sb1", "user-a"))
    await asyncio.sleep(0.02)
    r.store["attach:sb1"] = "user-b"  # a different viewer claimed it after user-a's TTL lapsed
    await asyncio.sleep(0.03)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert r.store["attach:sb1"] == "user-b"  # never overwritten back to user-a


# -- frame pump --------------------------------------------------------------------------


async def test_pump_relays_output_and_applies_client_stdin_and_resize_frames():
    ws = FakeWebSocket(
        [
            json.dumps({"type": "stdin", "data": base64.b64encode(b"ls\n").decode()}),
            json.dumps({"type": "resize", "cols": 80, "rows": 24}),
        ]
    )
    pty = FakePTYStream(
        [
            PTYEvent(kind="output", data=b"file.txt\n"),
            PTYEvent(kind="exit", exit_code=0),
        ]
    )

    await _pump(ws, pty)

    assert pty.written == [b"ls\n"]
    assert pty.resizes == [(80, 24)]
    assert ws.sent[0] == {"type": "stdout", "data": base64.b64encode(b"file.txt\n").decode("ascii")}
    assert ws.sent[1] == {"type": "exit", "exit_code": 0}


async def test_pump_maps_signal_frame_to_control_byte():
    ws = FakeWebSocket([json.dumps({"type": "signal", "signal": "SIGINT"})])
    pty = FakePTYStream([PTYEvent(kind="exit", exit_code=130)])

    await _pump(ws, pty)

    assert pty.written == [b"\x03"]  # Ctrl-C


async def test_pump_drops_malformed_client_frames_without_dying():
    ws = FakeWebSocket(["not json at all", json.dumps({"type": "unknown"})])
    pty = FakePTYStream([PTYEvent(kind="exit", exit_code=0)])

    await _pump(ws, pty)  # must not raise

    assert pty.written == []
