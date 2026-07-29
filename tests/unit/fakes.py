from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.domain.build import Artifact
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
        self.recycled: list[str] = []
        self.put_files_calls: list[dict[str, str]] = []
        self.attached: list[str] = []
        self._batch_result = batch_result
        self._raise_on_exec = raise_on_exec
        self._files = files or {}
        self._tree = tree or []
        self._pty_events = pty_events or []
        self._exec_in_result = exec_in_result
        self.raise_on_recycle: Exception | None = None
        self.archived_workspaces: list[str] = []
        self.deleted_workspace_volumes: list[str] = []
        self._archive_data = b"fake-tar-bytes"
        self.native_sandbox_refs: list = []
        self.measured_workspaces: list[str] = []
        self.usage_by_workspace: dict[str, int] = {}
        self.restored_workspaces: list[tuple[str, bytes]] = []

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
        if self.raise_on_recycle is not None:
            raise self.raise_on_recycle
        self.recycled.append(handle.sandbox_id)

    async def destroy(self, handle: SandboxHandle) -> None:
        self.destroyed.append(handle.sandbox_id)

    async def archive_workspace(self, workspace_id: str, *, archiver_image: str) -> bytes:
        self.archived_workspaces.append(workspace_id)
        return self._archive_data

    async def delete_workspace_volume(self, workspace_id: str) -> None:
        self.deleted_workspace_volumes.append(workspace_id)

    async def measure_workspace_usage(self, workspace_id: str, *, archiver_image: str) -> int:
        self.measured_workspaces.append(workspace_id)
        return self.usage_by_workspace.get(workspace_id, 0)

    async def restore_workspace(self, workspace_id: str, data: bytes, *, archiver_image: str) -> None:
        self.restored_workspaces.append((workspace_id, data))

    async def list_sandbox_refs(self):
        return self.native_sandbox_refs


class FakeImageRegistryProvider:
    """Records every push — used to assert BuildManager pushes exactly the artifact a
    strategy returned, without a real Docker daemon or registry."""

    def __init__(self, *, raise_on_push: Exception | None = None) -> None:
        self.pushed: list[str] = []
        self._raise_on_push = raise_on_push

    async def push(self, local_tag: str) -> str:
        self.pushed.append(local_tag)
        if self._raise_on_push is not None:
            raise self._raise_on_push
        return f"registry.local/{local_tag}"

    async def resolve(self, ref: str) -> str:
        return f"registry.local/{ref}"


class FakeObjectStorageProvider:
    """In-memory dict standing in for MinIO/Blob — get() raises KeyError for a missing
    key, matching ObjectStorageProvider's documented contract."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> None:
        self.store[key] = data

    async def get(self, key: str) -> bytes:
        if key not in self.store:
            raise KeyError(key)
        return self.store[key]

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


class FakeBuildStrategy:
    """Stands in for a real BuildStrategy (dockerfile/compose/pipeline/helm) in
    BuildManager tests — returns a canned Artifact or raises, and records every call."""

    def __init__(self, *, artifact: Artifact | None = None, raise_on_build: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._artifact = artifact or Artifact(kind="image", ref="fake/local:1.0")
        self._raise_on_build = raise_on_build

    async def build(self, component, ctx):
        self.calls.append(component.key)
        if self._raise_on_build is not None:
            raise self._raise_on_build
        return self._artifact
