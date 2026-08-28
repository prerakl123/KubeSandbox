"""Interactive PTY attach over WebSocket (doc §5.2) — the standalone-user path.

Strictly optional: install `kubesandbox-sdk[attach]` to pull in `websockets`. The
workflow-builder's code block never touches this (it's batch-only by design, doc §1),
so the base install stays httpx-only and importing this module without the extra raises
a clear message instead of an opaque `ModuleNotFoundError` on some line deep inside.

Wire protocol, mirroring `app/streaming/pty_protocol.py` exactly — JSON text frames
with base64 payloads:

* client -> server: `{"type": "stdin", "data": "<b64>"}`,
  `{"type": "resize", "cols": N, "rows": N}`, `{"type": "signal", "signal": "SIGINT"}`
* server -> client: `{"type": "stdout", "data": "<b64>"}`, `{"type": "exit",
  "exit_code": N}`, `{"type": "error", "message": "..."}`

The API key travels as `?api_key=` rather than a header, because a browser can't set
headers on a WebSocket handshake and the server's `get_ws_principal` reads the query
param for exactly that reason. That means the key lands in any URL logging along the
path — same tradeoff the control plane already made; over `wss://` the query string is
inside TLS, so use `wss://` in anything but local dev.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from types import TracebackType
from typing import AsyncIterator, Literal
from urllib.parse import quote, urlencode

from .errors import ConflictError, KubeSandboxError

try:  # pragma: no cover - exercised by whether the extra is installed
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]

_MISSING_EXTRA = (
    "PTY attach needs the 'websockets' package: pip install 'kubesandbox-sdk[attach]'"
)

SIGNALS = ("SIGINT", "SIGQUIT", "SIGTSTP")
"""The only signals a PTY can deliver (doc §5.2 / `pty_protocol._SIGNAL_BYTES`) — they
travel as terminal control bytes, so anything not mapped to one (SIGKILL, SIGTERM)
simply has no representation on this transport. Destroy the sandbox instead."""


@dataclass(frozen=True)
class PTYOutput:
    kind: Literal["stdout"]
    data: bytes
    """Raw bytes, not decoded text: a PTY emits a byte stream that can split a UTF-8
    sequence across chunks, so decoding is the consumer's problem (use an incremental
    decoder) — this SDK never guesses and never mangles."""


@dataclass(frozen=True)
class PTYExit:
    kind: Literal["exit"]
    exit_code: int


PTYEvent = PTYOutput | PTYExit


def _ws_url(base_url: str, sandbox_id: str, api_key: str | None) -> str:
    if base_url.startswith("https://"):
        url = "wss://" + base_url[len("https://") :]
    elif base_url.startswith("http://"):
        url = "ws://" + base_url[len("http://") :]
    else:
        raise KubeSandboxError(f"base_url must start with http:// or https://, got {base_url!r}")
    url = f"{url.rstrip('/')}/v1/sandboxes/{quote(sandbox_id)}/attach"
    if api_key:
        url = f"{url}?{urlencode({'api_key': api_key})}"
    return url


class PTYSession:
    """One live attach session. Exactly one viewer per sandbox (doc §5.2) — a second
    concurrent attach is refused by the server, surfacing here as `ConflictError`.

    ```python
    async with await attach(client, sandbox.id) as pty:
        await pty.send_stdin(b"echo hello\\n")
        async for event in pty:
            if event.kind == "exit":
                break
            sys.stdout.buffer.write(event.data)
    ```
    """

    def __init__(self, connection) -> None:
        self._ws = connection

    async def send_stdin(self, data: bytes) -> None:
        await self._ws.send(json.dumps({"type": "stdin", "data": base64.b64encode(data).decode("ascii")}))

    async def resize(self, *, cols: int, rows: int) -> None:
        await self._ws.send(json.dumps({"type": "resize", "cols": cols, "rows": rows}))

    async def send_signal(self, signal: str) -> None:
        if signal not in SIGNALS:
            raise KubeSandboxError(f"signal must be one of {SIGNALS}, got {signal!r}")
        await self._ws.send(json.dumps({"type": "signal", "signal": signal}))

    async def __aiter__(self) -> AsyncIterator[PTYEvent]:
        async for raw in self._ws:
            frame = json.loads(raw)
            kind = frame.get("type")
            if kind == "stdout":
                yield PTYOutput(kind="stdout", data=base64.b64decode(frame["data"]))
            elif kind == "exit":
                yield PTYExit(kind="exit", exit_code=frame["exit_code"])
            elif kind == "error":
                raise KubeSandboxError(f"server error frame: {frame.get('message')}")
            # Any other frame type is a newer server talking to an older SDK — dropped
            # rather than raised, so adding a server frame can't break existing clients.

    async def close(self) -> None:
        await self._ws.close()

    async def __aenter__(self) -> PTYSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


async def attach(client, sandbox_id: str) -> PTYSession:
    """Open an interactive PTY session against an existing sandbox.

    `client` is an `AsyncKubeSandboxClient` (its `base_url`/`api_key` are all this
    needs) — taken as a parameter rather than as a method on the client so the base
    install never imports `websockets` just because someone imported the client.

    Note: attach requires a sandbox created via `create_sandbox()`. `execute()`'s
    sandbox is ephemeral and already gone by the time it returns (doc §5.1).
    """
    if websockets is None:
        raise KubeSandboxError(_MISSING_EXTRA)
    url = _ws_url(client.base_url, sandbox_id, client.api_key)
    try:
        connection = await websockets.connect(url)
    except Exception as exc:  # noqa: BLE001 — normalized below
        # The server rejects a second viewer with a real HTTP 409 *during* the
        # handshake (raising HTTPException before accept(), doc §5.2's literal "409"),
        # which websockets surfaces as an InvalidStatus-family error rather than a
        # response object. Translated so callers catch the same ConflictError they'd
        # get from any other endpoint.
        if "409" in str(exc):
            raise ConflictError(409, "sandbox already has an active viewer") from exc
        raise
    return PTYSession(connection)
