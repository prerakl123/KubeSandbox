"""The seam that makes `local` (Docker) vs. `aks-prod` (Kubernetes) pluggable (doc §4.2).
SandboxService only ever talks to this Protocol — it never branches on backend."""

from __future__ import annotations

from typing import Protocol

from app.domain.execution import BatchCommand, BatchRunResult, SandboxHandle, SandboxSpec, SandboxStatus


class PTYStream(Protocol):
    """Interactive attach stream — Phase 4 (doc §5.2, roadmap §20). Not implemented by
    either provisioner yet; the shape is fixed now so the API layer can be built against
    it without a rewrite later."""

    async def write_stdin(self, data: bytes) -> None: ...
    async def read(self) -> bytes | None: ...
    async def resize(self, *, cols: int, rows: int) -> None: ...
    async def close(self) -> None: ...


class Provisioner(Protocol):
    async def acquire(self, spec: SandboxSpec) -> SandboxHandle:
        """Claim a pooled sandbox or create one from scratch and wait for readiness."""
        ...

    async def exec_batch(self, handle: SandboxHandle, command: BatchCommand) -> BatchRunResult:
        """Run one bundled batch command to completion (or timeout) and return the result."""
        ...

    async def attach(self, handle: SandboxHandle) -> PTYStream:
        """Open an interactive PTY session. Phase 4."""
        ...

    async def status(self, handle: SandboxHandle) -> SandboxStatus: ...

    async def put_files(self, handle: SandboxHandle, files: dict[str, str]) -> None: ...

    async def recycle(self, handle: SandboxHandle) -> None:
        """Wipe workspace state and return the sandbox to its warm pool (doc §4.3)."""
        ...

    async def destroy(self, handle: SandboxHandle) -> None:
        """Graceful teardown: must be idempotent and must never leak the underlying
        resource even if the sandbox is already gone or was never fully provisioned."""
        ...
