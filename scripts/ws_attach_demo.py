#!/usr/bin/env python3
"""Minimal interactive PTY attach demo/verification client for
`WS /v1/sandboxes/{id}/attach` (doc §5.2, Phase 4). Not part of the app itself — a
throwaway tool for manually exercising attach/resize/reattach/signal against a real
running control plane, since there's no browser terminal UI yet.

Usage:
    uv run python scripts/ws_attach_demo.py <sandbox_id> [ws://host:port]

Type commands and press Enter. Type `:sigint` + Enter to send a real SIGINT to the
remote foreground process (this demo's own convention, not part of the wire
protocol — see app/streaming/pty_protocol.py). Ctrl-D on *this* terminal closes the
demo (and, per the single-viewer lock's grace window, lets you reattach and find your
shell exactly where you left it).
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys

import websockets


async def _print_server_frames(ws) -> None:
    async for raw in ws:
        frame = json.loads(raw)
        if frame["type"] == "stdout":
            sys.stdout.buffer.write(base64.b64decode(frame["data"]))
            sys.stdout.flush()
        elif frame["type"] == "exit":
            print(f"\n[remote shell exited: {frame['exit_code']}]")
            return


async def _send_stdin_lines(ws) -> None:
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        if line.strip() == ":sigint":
            await ws.send(json.dumps({"type": "signal", "signal": "SIGINT"}))
            continue
        await ws.send(
            json.dumps({"type": "stdin", "data": base64.b64encode(line.encode()).decode("ascii")})
        )


async def main(sandbox_id: str, base_url: str) -> None:
    url = f"{base_url}/v1/sandboxes/{sandbox_id}/attach"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "resize", "cols": 120, "rows": 30}))
        done, pending = await asyncio.wait(
            {asyncio.create_task(_print_server_frames(ws)), asyncio.create_task(_send_stdin_lines(ws))},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <sandbox_id> [ws://host:port]", file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "ws://localhost:8000"))
