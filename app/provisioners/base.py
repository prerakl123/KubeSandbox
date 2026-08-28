"""The seam that makes `local` (Docker) vs. `aks-prod` (Kubernetes) pluggable (doc §4.2).
SandboxService only ever talks to this Protocol — it never branches on backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.execution import (
    BatchCommand,
    BatchRunResult,
    FileEntry,
    NativeSandboxRef,
    SandboxHandle,
    SandboxSpec,
    SandboxStatus,
)


@dataclass(frozen=True)
class PTYEvent:
    """One event off an interactive PTYStream (doc §5.2, Phase 4). `output` carries a
    raw byte chunk; a PTY merges stdout/stderr at the kernel level (there's no such
    thing as a separate stderr once a tty is allocated) so there's only one output
    kind, not two. Exactly one `exit` event ends the stream, mirroring how a real
    terminal session ends."""

    kind: Literal["output", "exit"]
    data: bytes | None = None
    exit_code: int | None = None


class PTYStream(Protocol):
    """Interactive attach stream (doc §5.2, roadmap Phase 4). `write_stdin` carries
    both real keystrokes and signal delivery — a PTY has no separate "send signal"
    verb; a foreground process receives SIGINT/SIGQUIT/SIGTSTP because the kernel's
    line discipline turns specific control bytes (Ctrl-C, Ctrl-\\, Ctrl-Z) written to
    the tty into those signals. This is standard PTY behavior (how `docker exec -t`,
    `kubectl exec -t`, and every real terminal deliver signals) — the WS gateway maps
    a client `signal` frame to the matching control byte before calling this."""

    async def write_stdin(self, data: bytes) -> None: ...
    async def read(self) -> PTYEvent | None:
        """None means the transport itself died without a clean exit event (should be
        rare — a clean session end always yields an `exit` PTYEvent first)."""
        ...

    async def resize(self, *, cols: int, rows: int) -> None: ...
    async def close(self) -> None: ...


class Provisioner(Protocol):
    backend_name: str
    """"docker" | "kubernetes" — the same value each implementation stamps onto the
    `SandboxHandle.backend` it returns, but readable *before* a handle exists. Added in
    Phase 9 for doc §14's `provision_latency`/`sandboxes_active` metrics, which need a
    `backend` label on the failure path too, where there is no handle to read it from.
    A plain class attribute rather than a property: it's a fixed per-implementation
    constant, never derived from instance state."""

    async def acquire(self, spec: SandboxSpec) -> SandboxHandle:
        """Claim a pooled sandbox or create one from scratch and wait for readiness."""
        ...

    async def exec_batch(self, handle: SandboxHandle, command: BatchCommand) -> BatchRunResult:
        """Run one bundled batch command to completion (or timeout) and return the result."""
        ...

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
        """Admin-exec targeting `"main"` or a sidecar name (doc §3.5, §20 Phase 5) —
        used by ComponentHooks (e.g. creating a scoped DB role) and healthcheck
        polling, never by sandboxed user code (that's exec_batch's job, always run as
        the sandbox's own restricted uid)."""
        ...

    async def attach(self, handle: SandboxHandle) -> PTYStream:
        """Open an interactive PTY session. Phase 4."""
        ...

    async def status(self, handle: SandboxHandle) -> SandboxStatus: ...

    async def put_files(self, handle: SandboxHandle, files: dict[str, str]) -> None: ...

    async def get_file(self, handle: SandboxHandle, path: str) -> bytes:
        """Download one file (doc §5.4) — `path` is an absolute in-sandbox path."""
        ...

    async def list_tree(self, handle: SandboxHandle, path: str) -> list[FileEntry]:
        """List everything under `path` (doc §5.4), non-recursive-vs-recursive left to
        the implementation — both current implementations return the full recursive
        listing since sandbox workspaces are small and bounded by quota."""
        ...


def parse_find_output(text: str) -> list[FileEntry]:
    """Parses `find <path> -mindepth 1 -printf '%y|%P\\n'` output — both provisioners'
    list_tree() shell out to that exact invocation, so they share this parser."""
    entries: list[FileEntry] = []
    for line in text.splitlines():
        if not line:
            continue
        kind, _, rel = line.partition("|")
        entries.append(FileEntry(path=rel, is_dir=kind == "d"))
    return entries

    async def recycle(self, handle: SandboxHandle) -> None:
        """Wipe workspace state and return the sandbox to its warm pool (doc §4.3)."""
        ...

    async def destroy(self, handle: SandboxHandle) -> None:
        """Graceful teardown: must be idempotent and must never leak the underlying
        resource even if the sandbox is already gone or was never fully provisioned."""
        ...

    async def archive_workspace(self, workspace_id: str, *, archiver_image: str) -> bytes:
        """Doc §10.2 retention (Phase 7): returns a tar of a persistent workspace's
        current contents — used with no sandbox currently attached to it (the
        workspace's own sandbox, if any, is always destroyed before archival runs).
        Implementations mount the durable volume/PVC into a short-lived, throwaway
        container/pod just for this, since there's no already-running sandbox to
        `exec` into by the time retention reaps an idle workspace. `archiver_image`
        is caller-resolved (the registry's `base` component image) rather than
        hardcoded here — a Provisioner has no dependency on the component registry
        anywhere else (doc §4.2's abstraction boundary), and a bare image string
        wouldn't be pullable as-is on every backend/registry combination anyway."""
        ...

    async def delete_workspace_volume(self, workspace_id: str) -> None:
        """Permanently removes a persistent workspace's durable volume/PVC — called
        right after a successful `archive_workspace()` upload (moving to cold
        storage) and is the terminal step of doc §10.2's retention path. Idempotent:
        a workspace with no volume/PVC left is already success, not an error."""
        ...

    async def measure_workspace_usage(self, workspace_id: str, *, archiver_image: str) -> int:
        """Current disk usage of a persistent workspace's durable volume/PVC, in MB —
        backs `WorkspaceService.check_quota()`'s enforcement. Same "no already-running
        sandbox to exec into" reasoning as `archive_workspace`, same caller-resolved
        `archiver_image` contract."""
        ...

    async def restore_workspace(self, workspace_id: str, data: bytes, *, archiver_image: str) -> None:
        """Symmetric to `archive_workspace()`: un-tars `data` (as produced by a prior
        `archive_workspace()` call) onto a fresh durable volume/PVC, recreating it if
        `delete_workspace_volume()` already removed it. Used to bring an `archived`
        workspace back to `active` (doc §10.2)."""
        ...

    async def list_sandbox_refs(self) -> list[NativeSandboxRef]:
        """Every currently-live *ephemeral* sandbox-labeled native resource (a main
        container for Docker, a namespace for Kubernetes) — the reconciler's orphan
        GC (doc §4.1, Phase 7) cross-references this against non-terminated `Sandbox`
        rows to find resources Postgres no longer knows about (a crash between
        acquire() and the row being committed, a control-plane restart mid-teardown,
        etc.) and removes them. Deliberately excludes persistent-workspace namespaces
        (those carry a `workspace-id` label, not a `sandbox-id` one) — their lifecycle
        is workspace retention's job, not orphan GC's."""
        ...
