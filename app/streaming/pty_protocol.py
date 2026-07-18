"""WS wire protocol for interactive PTY attach (doc §5.2, Phase 4). JSON text frames
with base64-encoded binary payloads — simple and symmetric across both provisioner
backends, at the cost of ~33% size overhead versus a raw binary framing (fine for an
interactive terminal's byte volumes).

Client frames: `stdin` | `resize` | `signal`. Server frames: `stdout` | `exit`.
"""

from __future__ import annotations

import base64
import json
from typing import Literal

from pydantic import BaseModel, ValidationError

# Signals deliverable via PTY control bytes (the `signal` client frame) — see
# app/provisioners/base.py's PTYStream docstring for why this is the whole transport,
# not a separate out-of-band verb: a PTY's line discipline turns these bytes into
# real signals for the foreground process, exactly like a real terminal.
_SIGNAL_BYTES: dict[str, bytes] = {
    "SIGINT": b"\x03",  # Ctrl-C
    "SIGQUIT": b"\x1c",  # Ctrl-\
    "SIGTSTP": b"\x1a",  # Ctrl-Z
}


class StdinFrame(BaseModel):
    type: Literal["stdin"] = "stdin"
    data: str  # base64

    def decode(self) -> bytes:
        return base64.b64decode(self.data)


class ResizeFrame(BaseModel):
    type: Literal["resize"] = "resize"
    cols: int
    rows: int


class SignalFrame(BaseModel):
    type: Literal["signal"] = "signal"
    signal: Literal["SIGINT", "SIGQUIT", "SIGTSTP"]

    def to_control_bytes(self) -> bytes:
        return _SIGNAL_BYTES[self.signal]


class OutputFrame(BaseModel):
    type: Literal["stdout"] = "stdout"
    data: str  # base64

    @classmethod
    def from_bytes(cls, data: bytes) -> OutputFrame:
        return cls(data=base64.b64encode(data).decode("ascii"))


class ExitFrame(BaseModel):
    type: Literal["exit"] = "exit"
    exit_code: int


class ErrorFrame(BaseModel):
    type: Literal["error"] = "error"
    message: str


ClientFrame = StdinFrame | ResizeFrame | SignalFrame
_CLIENT_FRAME_TYPES: dict[str, type[BaseModel]] = {
    "stdin": StdinFrame,
    "resize": ResizeFrame,
    "signal": SignalFrame,
}


def parse_client_frame(raw: str) -> ClientFrame:
    """Raises ValueError on anything malformed/unrecognized; the gateway drops such a
    frame and keeps the session alive rather than tearing down the whole attach over
    one bad message."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")

    model = _CLIENT_FRAME_TYPES.get(payload.get("type"))
    if model is None:
        raise ValueError(f"unknown client frame type: {payload.get('type')!r}")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
