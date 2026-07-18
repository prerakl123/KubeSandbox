from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.domain.execution import (
    BatchCommand,
    BatchRunResult,
    SandboxHandle,
    SandboxSpec,
    SandboxState,
    SandboxStatus,
)


class FakeProvisioner:
    """Records every call so tests can assert on provisioner interactions (e.g. that
    destroy() always runs) without needing a real Docker daemon."""

    def __init__(
        self,
        *,
        batch_result: BatchRunResult | None = None,
        raise_on_exec: Exception | None = None,
    ) -> None:
        self.acquired: list[SandboxSpec] = []
        self.exec_calls: list[BatchCommand] = []
        self.destroyed: list[str] = []
        self._batch_result = batch_result
        self._raise_on_exec = raise_on_exec

    async def acquire(self, spec: SandboxSpec) -> SandboxHandle:
        self.acquired.append(spec)
        return SandboxHandle(
            sandbox_id=str(uuid.uuid4()),
            backend="fake",
            native_ref="fake-container",
            created_at=datetime.now(UTC),
        )

    async def exec_batch(self, handle: SandboxHandle, command: BatchCommand) -> BatchRunResult:
        self.exec_calls.append(command)
        if self._raise_on_exec is not None:
            raise self._raise_on_exec
        return self._batch_result or BatchRunResult(
            run_id="fake-run", exit_code=0, stdout="", stderr="", duration_ms=1
        )

    async def attach(self, handle: SandboxHandle):
        raise NotImplementedError

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        return SandboxStatus(sandbox_id=handle.sandbox_id, state=SandboxState.ACTIVE)

    async def put_files(self, handle: SandboxHandle, files: dict[str, str]) -> None:
        pass

    async def recycle(self, handle: SandboxHandle) -> None:
        pass

    async def destroy(self, handle: SandboxHandle) -> None:
        self.destroyed.append(handle.sandbox_id)
