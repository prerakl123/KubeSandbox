"""Docker-backed Provisioner for the `local` environment (doc §4.2, §8.1).

Same interface as the future KubernetesProvisioner (Phase 3) — a sandbox is a single
already-running container ("sleep infinity") that batch commands are `exec`'d into,
mirroring how `kubectl exec` works against an already-running pod.

Notable implementation detail: batch stdin must be entirely up front, then EOF (doc
§5.1) — a program blocking on a read must see real EOF, not a stalled pipe, or it hangs
until the wall-clock timeout instead of finishing immediately. aiodocker's `Stream` has
no public "half-close stdin, keep reading stdout" primitive (`Stream.close()` tears down
the whole connection), so `_half_close_stdin` reaches into its internals to send a raw
TCP half-close. This is pinned to aiodocker==0.27.0 and is the single riskiest piece of
this provisioner — if it silently stops working on a future aiodocker upgrade, the
system still degrades safely (the exec's wall-clock timeout still reaps it), it just
stops being instant for programs that read stdin past the provided input.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from datetime import UTC, datetime

import aiodocker
from aiodocker.exceptions import DockerContainerError, DockerError
from aiodocker.stream import Stream

from app.core.errors import ProvisionerError, SandboxNotFoundError
from app.core.logging import get_logger
from app.domain.execution import (
    VARIABLE_DUMP_PATH,
    BatchCommand,
    BatchRunResult,
    FileEntry,
    SandboxHandle,
    SandboxSpec,
    SandboxState,
    SandboxStatus,
)
from app.provisioners.base import PTYEvent, PTYStream, parse_find_output
from app.provisioners.resources import parse_cpu_to_nanocpus, parse_memory_to_bytes

logger = get_logger(__name__)

_CONTAINER_NAME_PREFIX = "kubesandbox-"
# Fixed across every golden image (doc §6 Layer 1: runAsUser/Group: 10001, always).
_SANDBOX_UID = 10001
# aiodocker's container.exec() defaults to root when `user` is omitted (see its own
# docstring) — every exec here must be explicit, or "no root, ever" silently breaks.
_SANDBOX_EXEC_USER = f"{_SANDBOX_UID}:{_SANDBOX_UID}"
# `-A`: attach to this session if it already exists, else create it — a single command
# gives us create-or-reattach for free (doc §20 Phase 4 "reattach-after-disconnect").
# Requires `dtach` baked into the golden image (components/*/Dockerfile).
#
# Originally implemented with tmux, confirmed live NOT to work in these containers:
# tmux >=3.4 tries to move every new pane into its own cgroup via a systemd D-Bus
# call (`spawn_pane: moving pane to new cgroup failed: failed to connect to session
# bus: No medium found`), which is fatal here — no systemd/dbus exists in this
# minimal, non-root container, and adding one just to satisfy tmux would be a much
# bigger, security-relevant change than switching multiplexers. Confirmed live even a
# fully-detached `tmux new-session -d` session dies immediately (0 sessions after),
# ruling out a client-attach-specific issue. `dtach` has no such dependency — it's a
# single-purpose attach/detach wrapper around one pty, exactly what's needed here.
# `-e none`: disable dtach's own detach-escape-key handling entirely (default Ctrl-\)
# — SIGQUIT is delivered as that same control byte (see PTYStream docstring), and we
# don't want dtach intercepting it; detach only ever happens via the WS connection
# actually dropping. `-z`: pass Ctrl-Z straight through to the shell instead of dtach
# swallowing it as a host-side suspend key (there's no real host job control here).
_INTERACTIVE_SHELL_CMD = ["dtach", "-A", "/tmp/.kubesandbox-attach.sock", "-e", "none", "-z", "/bin/bash"]


def _half_close_stdin(stream: Stream) -> None:
    """Send a TCP half-close (FIN) on our write side only, so the exec'd process sees
    stdin EOF while we keep reading stdout/stderr. See module docstring."""
    resp = stream._resp  # noqa: SLF001 — no public API for this, see module docstring
    if resp is None or resp.connection is None:
        return
    transport = resp.connection.transport
    if transport is not None and transport.can_write_eof():
        with contextlib.suppress(RuntimeError):
            transport.write_eof()


class DockerProvisioner:
    def __init__(self, docker: aiodocker.Docker | None = None) -> None:
        self._docker = docker or aiodocker.Docker()
        self._owns_docker = docker is None

    async def aclose(self) -> None:
        if self._owns_docker:
            await self._docker.close()

    # -- lifecycle -------------------------------------------------------------------

    async def acquire(self, spec: SandboxSpec) -> SandboxHandle:
        sandbox_id = str(uuid.uuid4())
        config = {
            "Image": spec.image,
            "Cmd": ["sleep", "infinity"],
            "WorkingDir": spec.workdir,
            "Env": [f"{k}={v}" for k, v in spec.env.items()],
            "Labels": {**spec.labels, "io.kubesandbox.sandbox-id": sandbox_id},
            "HostConfig": {
                "NanoCpus": parse_cpu_to_nanocpus(spec.resources.cpu),
                "Memory": parse_memory_to_bytes(spec.resources.memory),
                "PidsLimit": spec.max_processes,
                "ReadonlyRootfs": spec.read_only_root_filesystem,
                # No persistent workspace support yet (Phase 7) — writable paths are
                # tmpfs, which also makes recycle() (wipe workspace) trivial. Explicit
                # uid/gid/mode: an unqualified tmpfs mount defaults to root-owned,
                # which the sandbox's non-root uid then can't write into (confirmed
                # against a live daemon — "Permission denied" writing into /workspace).
                # Explicit "exec": confirmed against a live daemon that Docker silently
                # mounts an unqualified tmpfs `noexec` — harmless for interpreted
                # languages (they never execve anything out of /workspace or /tmp) but
                # it breaks every compiled-language component (Go's `go run` compiles a
                # fresh binary into $GOTMPDIR/$GOCACHE, both under /tmp, then execve's
                # it — "permission denied" with no other symptom). Doesn't weaken
                # containment: the sandboxed non-root user can already run arbitrary
                # code via the language interpreter/compiler itself.
                "Tmpfs": {
                    path: f"rw,exec,nosuid,nodev,size=1g,uid={_SANDBOX_UID},gid={_SANDBOX_UID},mode=0755"
                    for path in spec.writable_paths
                },
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                # Default-deny is the floor everywhere (doc §12); local has no
                # mirror/egress proxy wired up yet, so full isolation is correct here.
                "NetworkMode": "none",
            },
        }

        try:
            container = await self._docker.containers.run(
                config, name=f"{_CONTAINER_NAME_PREFIX}{sandbox_id}"
            )
        except DockerContainerError as exc:
            # `run()` creates then starts; if start fails the container it created is
            # NOT cleaned up by aiodocker. Graceful eradication means we never leak it.
            await self._force_remove(exc.container_id)
            raise ProvisionerError(f"sandbox container failed to start: {exc}") from exc
        except DockerError as exc:
            raise ProvisionerError(f"failed to create sandbox container: {exc}") from exc

        return SandboxHandle(
            sandbox_id=sandbox_id,
            backend="docker",
            native_ref=container.id,
            created_at=datetime.now(UTC),
        )

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        container = self._docker.containers.container(handle.native_ref)
        try:
            info = await container.show()
        except DockerError:
            return SandboxStatus(sandbox_id=handle.sandbox_id, state=SandboxState.TERMINATED)

        state = info.get("State", {})
        if state.get("Running"):
            mapped = SandboxState.ACTIVE
        elif state.get("Status") == "created":
            mapped = SandboxState.PROVISIONING
        else:
            mapped = SandboxState.TERMINATED
        return SandboxStatus(sandbox_id=handle.sandbox_id, state=mapped, detail=state.get("Status"))

    async def recycle(self, handle: SandboxHandle) -> None:
        """Wipe /workspace and confirm the container is still healthy (doc §4.3).
        Not wired to a real PoolManager yet (Phase 7) — SandboxService currently always
        destroys ephemeral sandboxes instead of recycling them."""
        container = self._docker.containers.container(handle.native_ref)
        try:
            exec_obj = await container.exec(
                cmd=["sh", "-c", "rm -rf /workspace/* /workspace/.[!.]* 2>/dev/null; true"],
                stdout=True,
                stderr=True,
                user=_SANDBOX_EXEC_USER,
            )
            async with exec_obj.start(detach=False) as stream:
                while await stream.read_out() is not None:
                    pass
        except DockerError as exc:
            raise ProvisionerError(f"failed to recycle sandbox {handle.sandbox_id}: {exc}") from exc

    async def destroy(self, handle: SandboxHandle) -> None:
        """Graceful eradication: SIGTERM with a bounded grace period, then a forced
        remove regardless, so a slow/hung process can never leak a container. Safe to
        call more than once — "already gone" is treated as success, not an error."""
        container = self._docker.containers.container(handle.native_ref)
        try:
            await container.stop(t=5)
        except DockerError as exc:
            if exc.status != 404:
                logger.warning("sandbox_graceful_stop_failed", sandbox_id=handle.sandbox_id, error=str(exc))

        try:
            await container.delete(force=True, v=True)
        except DockerError as exc:
            if exc.status == 404:
                return
            raise ProvisionerError(f"failed to destroy sandbox {handle.sandbox_id}: {exc}") from exc

    async def _force_remove(self, native_ref: str) -> None:
        container = self._docker.containers.container(native_ref)
        with contextlib.suppress(DockerError):
            await container.delete(force=True, v=True)

    # -- files -------------------------------------------------------------------------

    async def put_files(self, handle: SandboxHandle, files: dict[str, str]) -> None:
        """Writes via `exec` rather than the archive/`docker cp` API: confirmed against
        a live daemon that `put_archive` refuses outright on any container created with
        ReadonlyRootfs=true — even when the destination is a separate writable tmpfs
        mount (a Docker daemon limitation specific to that copy-in endpoint, not to
        writes in general). A process exec'd into the container can write into that
        same tmpfs mount with no such restriction."""
        if not files:
            return
        container = self._docker.containers.container(handle.native_ref)
        for rel_path, content in files.items():
            await self._write_file_via_exec(container, rel_path, content)

    async def _write_file_via_exec(
        self, container: aiodocker.docker.DockerContainer, rel_path: str, content: str
    ) -> None:
        try:
            exec_obj = await container.exec(
                cmd=["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "--", rel_path],
                stdin=True,
                stdout=True,
                stderr=True,
                tty=False,
                workdir="/workspace",
                user=_SANDBOX_EXEC_USER,
            )
        except DockerError as exc:
            raise ProvisionerError(f"failed to open file write for {rel_path!r}: {exc}") from exc

        stream = exec_obj.start(detach=False)
        output = bytearray()
        try:
            await stream._init()  # noqa: SLF001 — must run before write_in/half-close
            await stream.write_in(content.encode("utf-8"))
            _half_close_stdin(stream)
            while True:
                msg = await stream.read_out()
                if msg is None:
                    break
                output.extend(msg.data)
        finally:
            with contextlib.suppress(Exception):
                await stream.close()

        info = await exec_obj.inspect()
        if info.get("ExitCode"):
            raise ProvisionerError(
                f"failed to write file {rel_path!r} into sandbox "
                f"(exit {info.get('ExitCode')}): {output.decode(errors='replace')!r}"
            )

    # -- batch execution ----------------------------------------------------------------

    async def exec_batch(self, handle: SandboxHandle, command: BatchCommand) -> BatchRunResult:
        run_id = str(uuid.uuid4())
        container = self._docker.containers.container(handle.native_ref)

        if command.files:
            await self.put_files(handle, command.files)

        try:
            exec_obj = await container.exec(
                cmd=command.command,
                stdin=True,
                stdout=True,
                stderr=True,
                tty=False,
                workdir="/workspace",
                user=_SANDBOX_EXEC_USER,
            )
        except DockerError as exc:
            if exc.status == 404:
                raise SandboxNotFoundError(handle.sandbox_id) from exc
            raise ProvisionerError(f"failed to start batch exec: {exc}") from exc

        stream = exec_obj.start(detach=False)
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        truncated = False
        timed_out = False
        start = time.monotonic()

        async def _pump() -> None:
            nonlocal truncated
            await stream._init()  # noqa: SLF001 — must run before write_in/half-close
            if command.stdin:
                await stream.write_in(command.stdin.encode())
            _half_close_stdin(stream)  # signal stdin EOF without closing the read side

            while True:
                msg = await stream.read_out()
                if msg is None:
                    return
                buf = stderr_buf if msg.stream == 2 else stdout_buf
                if len(buf) < command.max_output_bytes:
                    buf.extend(msg.data)
                else:
                    truncated = True  # keep draining so we still learn the real exit code

        try:
            await asyncio.wait_for(_pump(), timeout=command.timeout_seconds)
        except TimeoutError:
            timed_out = True
        finally:
            with contextlib.suppress(Exception):
                await stream.close()

        duration_ms = int((time.monotonic() - start) * 1000)

        exit_code = 124 if timed_out else 0  # 124 mirrors coreutils' `timeout` convention
        if not timed_out:
            with contextlib.suppress(DockerError):
                info = await exec_obj.inspect()
                exit_code = info.get("ExitCode") or 0

        variables = await self._read_variable_dump(container) if command.capture_variables else None

        return BatchRunResult(
            run_id=run_id,
            exit_code=exit_code,
            stdout=stdout_buf.decode(errors="replace"),
            stderr=stderr_buf.decode(errors="replace"),
            duration_ms=duration_ms,
            truncated=truncated,
            timed_out=timed_out,
            variables=variables,
        )

    async def _exec_capture(
        self, container: aiodocker.docker.DockerContainer, cmd: list[str]
    ) -> tuple[int, bytes]:
        """Run `cmd` via `exec` and capture its combined stdout+stderr as raw bytes —
        shared by variable-dump reading, file download, and tree listing, all of which
        read via `exec` (`cat`/`find`) rather than the archive/`docker cp` API:
        confirmed against a live daemon that `get_archive` can't find a file written
        into a tmpfs mount on a ReadonlyRootfs container (the same archive-endpoint
        limitation already worked around in `put_files`), even though the file
        demonstrably exists from the writer's own point of view."""
        exec_obj = await container.exec(cmd=cmd, stdout=True, stderr=True, user=_SANDBOX_EXEC_USER)
        output = bytearray()
        stream = exec_obj.start(detach=False)
        try:
            while True:
                msg = await stream.read_out()
                if msg is None:
                    break
                output.extend(msg.data)
        finally:
            with contextlib.suppress(Exception):
                await stream.close()
        info = await exec_obj.inspect()
        return info.get("ExitCode") or 0, bytes(output)

    async def _read_variable_dump(self, container: aiodocker.docker.DockerContainer) -> dict | None:
        try:
            exit_code, output = await self._exec_capture(container, ["cat", VARIABLE_DUMP_PATH])
        except DockerError as exc:
            logger.warning("variable_dump_read_failed", stage="exec_create", error=str(exc))
            return None

        if exit_code:
            logger.warning("variable_dump_read_failed", stage="cat_exit", exit_code=exit_code)
            return None

        try:
            return json.loads(output.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning("variable_dump_read_failed", stage="parse", error=str(exc))
            return None

    async def get_file(self, handle: SandboxHandle, path: str) -> bytes:
        container = self._docker.containers.container(handle.native_ref)
        try:
            exit_code, output = await self._exec_capture(container, ["cat", path])
        except DockerError as exc:
            raise ProvisionerError(f"failed to read {path!r}: {exc}") from exc
        if exit_code:
            raise ProvisionerError(
                f"failed to read {path!r} (exit {exit_code}): {output.decode(errors='replace')!r}"
            )
        return output

    async def list_tree(self, handle: SandboxHandle, path: str) -> list[FileEntry]:
        container = self._docker.containers.container(handle.native_ref)
        cmd = ["find", path, "-mindepth", "1", "-printf", "%y|%P\n"]
        try:
            exit_code, output = await self._exec_capture(container, cmd)
        except DockerError as exc:
            raise ProvisionerError(f"failed to list {path!r}: {exc}") from exc
        if exit_code:
            raise ProvisionerError(
                f"failed to list {path!r} (exit {exit_code}): {output.decode(errors='replace')!r}"
            )
        return parse_find_output(output.decode(errors="replace"))

    # -- interactive (Phase 4) -----------------------------------------------------------

    async def attach(self, handle: SandboxHandle) -> PTYStream:
        container = self._docker.containers.container(handle.native_ref)
        try:
            exec_obj = await container.exec(
                cmd=_INTERACTIVE_SHELL_CMD,
                stdin=True,
                stdout=True,
                stderr=True,
                tty=True,
                workdir="/workspace",
                user=_SANDBOX_EXEC_USER,
            )
        except DockerError as exc:
            if exc.status == 404:
                raise SandboxNotFoundError(handle.sandbox_id) from exc
            raise ProvisionerError(f"failed to open interactive attach: {exc}") from exc

        stream = exec_obj.start(detach=False)
        # Single explicit init point before read_out()/write_in() might run
        # concurrently from separate WS-gateway pump tasks — both methods lazily call
        # _init() themselves, but only the first caller should race the handshake;
        # same reasoning as exec_batch's _pump doing this before write_in/half-close.
        await stream._init()  # noqa: SLF001 — no public API for this, see docker.py module docstring
        return DockerPTYStream(exec_obj, stream)


class DockerPTYStream:
    """Wraps an aiodocker tty=True Exec/Stream pair as a PTYStream (doc §5.2). Under
    tty=True, aiodocker's own parser (`_ExecParser`) never multiplexes stdout/stderr —
    it hands back everything as one raw channel (see aiodocker/stream.py), matching
    real PTY semantics (stdout+stderr are the same fd once a tty is allocated). Unlike
    batch exec, there's no stream-embedded exit status either way; the exit code is
    only available via `exec_obj.inspect()` once the stream reports EOF."""

    def __init__(self, exec_obj: aiodocker.execs.Exec, stream: Stream) -> None:
        self._exec_obj = exec_obj
        self._stream = stream
        self._exited = False

    async def write_stdin(self, data: bytes) -> None:
        await self._stream.write_in(data)

    async def resize(self, *, cols: int, rows: int) -> None:
        await self._exec_obj.resize(h=rows, w=cols)

    async def read(self) -> PTYEvent | None:
        if self._exited:
            return None
        msg = await self._stream.read_out()
        if msg is None:
            self._exited = True
            with contextlib.suppress(DockerError):
                info = await self._exec_obj.inspect()
                return PTYEvent(kind="exit", exit_code=info.get("ExitCode") or 0)
            return None
        return PTYEvent(kind="output", data=msg.data)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._stream.close()
