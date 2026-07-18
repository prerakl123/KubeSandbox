from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.domain.execution import (
    BatchCommand,
    BatchRunResult,
    FileEntry,
    SandboxHandle,
    SandboxSpec,
    SandboxState,
    SandboxStatus,
)
from app.provisioners.base import PTYEvent


class FakePTYStream:
    """Records writes/resizes and replays a scripted list of PTYEvents — enough to
    drive the WS gateway's pump logic in tests without a real Docker/K8s exec."""

    def __init__(self, events: list[PTYEvent] | None = None) -> None:
        self.written: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.closed = False
        self._events = list(events or [])

    async def write_stdin(self, data: bytes) -> None:
        self.written.append(data)

    async def resize(self, *, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))

    async def read(self) -> PTYEvent | None:
        if not self._events:
            return None
        return self._events.pop(0)

    async def close(self) -> None:
        self.closed = True


class FakeProvisioner:
    """Records every call so tests can assert on provisioner interactions (e.g. that
    destroy() always runs) without needing a real Docker daemon."""

    def __init__(
        self,
        *,
        batch_result: BatchRunResult | None = None,
        raise_on_exec: Exception | None = None,
        files: dict[str, bytes] | None = None,
        tree: list[FileEntry] | None = None,
        pty_events: list[PTYEvent] | None = None,
        exec_in_result: BatchRunResult | None = None,
    ) -> None:
        self.acquired: list[SandboxSpec] = []
        self.exec_calls: list[BatchCommand] = []
        self.exec_in_calls: list[tuple[str, list[str]]] = []
        self.destroyed: list[str] = []
        self.put_files_calls: list[dict[str, str]] = []
        self.attached: list[str] = []
        self._batch_result = batch_result
        self._raise_on_exec = raise_on_exec
        self._files = files or {}
        self._tree = tree or []
        self._pty_events = pty_events or []
        self._exec_in_result = exec_in_result

    async def acquire(self, spec: SandboxSpec) -> SandboxHandle:
        self.acquired.append(spec)
        return SandboxHandle(
            sandbox_id=str(uuid.uuid4()),
            backend="fake",
            native_ref="fake-container",
            created_at=datetime.now(UTC),
            sidecar_refs={s.name: f"fake-sidecar-{s.name}" for s in spec.sidecars},
        )

    async def exec_batch(self, handle: SandboxHandle, command: BatchCommand) -> BatchRunResult:
        self.exec_calls.append(command)
        if self._raise_on_exec is not None:
            raise self._raise_on_exec
        return self._batch_result or BatchRunResult(
            run_id="fake-run", exit_code=0, stdout="", stderr="", duration_ms=1
        )

    async def exec_in(
        self,
        handle: SandboxHandle,
        target: str,
        command: list[str],
        *,
        stdin: bytes = b"",
        timeout_seconds: int = 30,
        max_output_bytes: int = 1_000_000,
    ) -> BatchRunResult:
        self.exec_in_calls.append((target, command))
        if self._raise_on_exec is not None:
            raise self._raise_on_exec
        return self._exec_in_result or BatchRunResult(
            run_id="fake-exec-in", exit_code=0, stdout="", stderr="", duration_ms=1
        )

    async def attach(self, handle: SandboxHandle) -> FakePTYStream:
        self.attached.append(handle.sandbox_id)
        return FakePTYStream(list(self._pty_events))

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        return SandboxStatus(sandbox_id=handle.sandbox_id, state=SandboxState.ACTIVE)

    async def put_files(self, handle: SandboxHandle, files: dict[str, str]) -> None:
        self.put_files_calls.append(files)

    async def get_file(self, handle: SandboxHandle, path: str) -> bytes:
        return self._files[path]

    async def list_tree(self, handle: SandboxHandle, path: str) -> list[FileEntry]:
        return self._tree

    async def recycle(self, handle: SandboxHandle) -> None:
        pass

    async def destroy(self, handle: SandboxHandle) -> None:
        self.destroyed.append(handle.sandbox_id)
