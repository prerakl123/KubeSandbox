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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import aiodocker
from aiodocker.exceptions import DockerContainerError, DockerError
from aiodocker.stream import Stream
from aiodocker.utils import clean_filters
from aiodocker.volumes import DockerVolume

from app.core.errors import ProvisionerError, SandboxNotFoundError
from app.core.logging import get_logger
from app.domain.execution import (
    VARIABLE_DUMP_PATH,
    BatchCommand,
    BatchRunResult,
    FileEntry,
    NativeSandboxRef,
    SandboxHandle,
    SandboxSpec,
    SandboxState,
    SandboxStatus,
    SidecarSpec,
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

# Docker has no pod-readiness-probe equivalent for a bare container (doc §20 Phase 5) —
# a sidecar's health_check is polled via exec instead, bounded by these. 90s (not 30s):
# confirmed live that MySQL 8's InnoDB initialization alone can take 30s+ under a
# constrained cpu limit (0.5 here) — under 2s unconstrained — so the original 30s
# window was routinely too tight, timing out mid-legitimate-startup rather than
# catching an actual failure. Matches (and exceeds, for margin) the ~60s Kubernetes'
# own readinessProbe settings already allow for the same sidecars (see
# KubernetesProvisioner._sidecar_container's initial_delay/period/failure_threshold).
_SIDECAR_HEALTH_TIMEOUT_SECONDS = 90
_SIDECAR_HEALTH_POLL_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True)
class _RawExecResult:
    """Same shape as BatchRunResult minus the fields only the caller knows (run_id)
    and with stdout/stderr as raw bytes rather than lossily-decoded str — mirrors
    kubernetes.py's dataclass of the same name/purpose."""

    exit_code: int
    stdout: bytes
    stderr: bytes
    duration_ms: int
    truncated: bool
    timed_out: bool


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
        persistent = spec.workspace_id is not None
        volume_name = self._workspace_volume_name(spec.workspace_id) if persistent else None
        if volume_name is not None:
            # Docker's "create" is idempotent by name — if it already exists (a
            # returning workspace), the API returns the existing volume unmodified
            # rather than erroring (confirmed against the Engine API docs, not
            # guessed; same "safe to always call" shape as recycle()'s rm -rf).
            await self._docker.volumes.create({"Name": volume_name})

        # Persistent workspaces mount a named volume at workdir instead of tmpfs — a
        # named volume outlives this container (and destroy()'s `v=True`, which only
        # reaps *anonymous* volumes, never a named one). Every other writable path
        # (e.g. /tmp) stays tmpfs regardless, since only the workspace itself needs to
        # survive across sessions.
        tmpfs_paths = [p for p in spec.writable_paths if not (persistent and p == spec.workdir)]
        host_config = {
            "NanoCpus": parse_cpu_to_nanocpus(spec.resources.cpu),
            "Memory": parse_memory_to_bytes(spec.resources.memory),
            "PidsLimit": spec.max_processes,
            "ReadonlyRootfs": spec.read_only_root_filesystem,
            # Explicit uid/gid/mode: an unqualified tmpfs mount defaults to root-owned,
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
                for path in tmpfs_paths
            },
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            # Default-deny is the floor everywhere (doc §12); local has no
            # mirror/egress proxy wired up yet, so full isolation is correct here.
            "NetworkMode": "none",
        }
        if volume_name is not None:
            host_config["Mounts"] = [
                {"Type": "volume", "Source": volume_name, "Target": spec.workdir, "ReadOnly": False}
            ]
            # A fresh named volume's mount point is root-owned, same problem tmpfs
            # would have without the uid=/gid= options above — but a regular volume
            # mount has no such option, so ownership is fixed with one root exec
            # right after start instead (see below). CHOWN is the one capability that
            # exec needs for it (doc §16/Phase 5's identical "capabilities gate root's
            # own powers too" finding) — doesn't expose anything to sandboxed user
            # code, which always execs as the non-root _SANDBOX_EXEC_USER explicitly,
            # never as root, so this capability sits unused in its bounding set.
            host_config["CapAdd"] = ["CHOWN"]

        config = {
            "Image": spec.image,
            "Cmd": ["sleep", "infinity"],
            "WorkingDir": spec.workdir,
            "Env": [f"{k}={v}" for k, v in spec.env.items()],
            "Labels": {**spec.labels, "io.kubesandbox.sandbox-id": sandbox_id},
            "HostConfig": host_config,
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

        try:
            await self._wait_container_running(container)
            if volume_name is not None:
                await self._fix_workspace_ownership(container, spec.workdir)
        except ProvisionerError:
            await self._force_remove(container.id)
            raise

        sidecar_refs: dict[str, str] = {}
        try:
            for sidecar in spec.sidecars:
                sidecar_container = await self._create_sidecar(sandbox_id, container.id, sidecar)
                # Recorded BEFORE the health-check wait below: a sidecar that starts
                # but never becomes healthy is a container that genuinely exists and
                # must still be found and removed by the except block's cleanup loop
                # — recording only on full (create-and-healthy) success previously
                # left an unhealthy sidecar's id out of this dict entirely, leaking
                # the container (confirmed live: two crashed `-postgresql` containers
                # survived cleanup because of exactly this ordering bug).
                sidecar_refs[sidecar.name] = sidecar_container.id
                if sidecar.health_check:
                    await self._wait_sidecar_healthy(sidecar_container, sidecar.name, sidecar.health_check)
        except Exception as exc:
            # A failed sidecar must not leak the sidecars that DID start, or main.
            for sidecar_id in sidecar_refs.values():
                await self._force_remove(sidecar_id)
            await self._force_remove(container.id)
            if isinstance(exc, ProvisionerError):
                raise
            raise ProvisionerError(
                f"failed to provision sidecar for sandbox {sandbox_id}: {exc}"
            ) from exc

        return SandboxHandle(
            sandbox_id=sandbox_id,
            backend="docker",
            native_ref=container.id,
            created_at=datetime.now(UTC),
            sidecar_refs=sidecar_refs,
            persistent=persistent,
        )

    @staticmethod
    def _workspace_volume_name(workspace_id: str) -> str:
        return f"kubesandbox-ws-{workspace_id}"

    async def _fix_workspace_ownership(
        self, container: aiodocker.docker.DockerContainer, workdir: str
    ) -> None:
        """A brand-new named volume's mount point is root-owned; idempotent to run on
        every acquire() (a non-recursive chown of just the mount point itself, not
        `-R` — cheap regardless of how much the workspace already holds, and a no-op
        once it's already 10001:10001 from a prior session)."""
        exec_obj = await container.exec(
            cmd=["chown", _SANDBOX_EXEC_USER, workdir], stdout=True, stderr=True, user="0:0"
        )
        async with exec_obj.start(detach=False) as stream:
            output = b""
            while (msg := await stream.read_out()) is not None:
                output += msg.data
        info = await exec_obj.inspect()
        if info.get("ExitCode"):
            raise ProvisionerError(
                f"failed to fix ownership of persistent workspace at {workdir}: {output!r}"
            )

    async def _wait_container_running(
        self, container: aiodocker.docker.DockerContainer, *, timeout_seconds: float = 10.0
    ) -> None:
        """`containers.run()` (create+start) can return before the container has
        actually settled into Docker's "running" state — invisible for every
        already-built, previously-run golden image (python/node/go/base — the Docker
        daemon already has every layer extracted and snapshotted from prior runs), but
        confirmed live to be a real race the *very first* time a container is created
        from a freshly built image (Phase 6's `jq`/`ripgrep`, built moments earlier via
        BuildManager): the immediately-following `put_files` exec hit a genuine `409
        container ... is not running`, not a flaky one-off. Bounded poll, not a fixed
        sleep — the daemon is usually fast, this only pays the cost when it isn't."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            info = await container.show()
            if info.get("State", {}).get("Running"):
                return
            if loop.time() >= deadline:
                raise ProvisionerError(
                    f"sandbox container {container.id} did not reach 'running' state "
                    f"within {timeout_seconds}s (state: {info.get('State')})"
                )
            await asyncio.sleep(0.05)

    async def _create_sidecar(
        self, sandbox_id: str, main_container_id: str, sidecar: SidecarSpec
    ) -> aiodocker.docker.DockerContainer:
        """One additional container sharing main's network namespace via
        `network_mode: container:<id>` — the Docker analog of "same pod" (doc §3.3's
        `reachableFrom: same-pod-only`). Main's own NetworkMode is already "none" (no
        external connectivity, see the main HostConfig above), so this preserves that
        guarantee while letting main reach the sidecar over localhost.

        Deliberately less locked-down than main — no ReadonlyRootfs, no forced sandbox
        uid: a sidecar is a vetted database image, not sandboxed user code, and images
        like Postgres/MySQL/Redis need to write well beyond one declared data
        directory (sockets, logs, temp files) just to start up.

        CapAdd restores exactly the 5 capabilities these official images' own
        entrypoint scripts need for their standard root-bootstrap-then-drop-privileges
        pattern (start as PID 1 root, `chown`/`chmod` the data directory to their own
        service uid, then `gosu <service-user>` to actually run the server) — CHOWN/
        FOWNER/DAC_OVERRIDE for the chown+chmod, SETUID/SETGID for gosu's privilege
        drop. Confirmed live: dropping ALL capabilities (this container's original,
        more-hardened-than-necessary posture) breaks that bootstrap outright —
        `chown: /var/lib/postgresql/data: Operation not permitted` even though the
        process is uid 0, because capabilities gate root's own powers too, not just
        non-root users. No broader loosening than these 5 specific capabilities.
        """
        config = {
            "Image": sidecar.image,
            "Env": [f"{k}={v}" for k, v in sidecar.env.items()],
            "Labels": {
                "io.kubesandbox.sandbox-id": sandbox_id,
                "io.kubesandbox.sidecar": sidecar.name,
            },
            "HostConfig": {
                "NetworkMode": f"container:{main_container_id}",
                "NanoCpus": parse_cpu_to_nanocpus(sidecar.resources.cpu),
                "Memory": parse_memory_to_bytes(sidecar.resources.memory),
                "PidsLimit": sidecar.max_processes,
                "Tmpfs": {
                    path: f"rw,exec,nosuid,nodev,size=1g,uid={sidecar.uid},gid={sidecar.uid},mode=0755"
                    for path in sidecar.writable_paths
                },
                "CapDrop": ["ALL"],
                "CapAdd": ["CHOWN", "FOWNER", "DAC_OVERRIDE", "SETUID", "SETGID"],
                "SecurityOpt": ["no-new-privileges"],
            },
        }
        try:
            sidecar_container = await self._docker.containers.run(
                config, name=f"{_CONTAINER_NAME_PREFIX}{sandbox_id}-{sidecar.name}"
            )
        except DockerContainerError as exc:
            await self._force_remove(exc.container_id)
            raise ProvisionerError(f"sidecar {sidecar.name!r} failed to start: {exc}") from exc
        except DockerError as exc:
            raise ProvisionerError(f"failed to create sidecar {sidecar.name!r}: {exc}") from exc

        return sidecar_container

    async def _wait_sidecar_healthy(
        self, container: aiodocker.docker.DockerContainer, name: str, health_check: list[str]
    ) -> None:
        """Polls `health_check` via exec until it exits 0 — stands in for what
        KubernetesProvisioner's _wait_ready gets from a readinessProbe for free;
        Docker has no bare-container equivalent of pod readiness."""
        deadline = time.monotonic() + _SIDECAR_HEALTH_TIMEOUT_SECONDS
        last_output = b""
        while time.monotonic() < deadline:
            with contextlib.suppress(DockerError):
                exit_code, last_output = await self._exec_capture(container, health_check, user=None)
                if exit_code == 0:
                    return
            await asyncio.sleep(_SIDECAR_HEALTH_POLL_INTERVAL_SECONDS)
        raise ProvisionerError(
            f"sidecar {name!r} did not become healthy within "
            f"{_SIDECAR_HEALTH_TIMEOUT_SECONDS}s: {last_output.decode(errors='replace')!r}"
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
        call more than once — "already gone" is treated as success, not an error.

        Sidecars are torn down before main: a container using `network_mode:
        container:<main>` can be left in a broken/orphaned state if main disappears
        out from under it first, so removing them first avoids depending on that.
        Sidecar teardown is best-effort (never blocks main's own destruction on a
        misbehaving sidecar) — main teardown is not.
        """
        for sidecar_id in handle.sidecar_refs.values():
            await self._destroy_one(sidecar_id, sandbox_id=handle.sandbox_id, best_effort=True)
        await self._destroy_one(handle.native_ref, sandbox_id=handle.sandbox_id, best_effort=False)

    async def _destroy_one(self, native_ref: str, *, sandbox_id: str, best_effort: bool) -> None:
        container = self._docker.containers.container(native_ref)
        try:
            await container.stop(t=5)
        except DockerError as exc:
            if exc.status != 404:
                logger.warning("sandbox_graceful_stop_failed", sandbox_id=sandbox_id, error=str(exc))

        try:
            await container.delete(force=True, v=True)
        except DockerError as exc:
            if exc.status == 404:
                return
            if best_effort:
                logger.warning("sandbox_sidecar_destroy_failed", sandbox_id=sandbox_id, error=str(exc))
                return
            raise ProvisionerError(f"failed to destroy sandbox {sandbox_id}: {exc}") from exc

    async def _force_remove(self, native_ref: str) -> None:
        container = self._docker.containers.container(native_ref)
        with contextlib.suppress(DockerError):
            await container.delete(force=True, v=True)

    # -- persistent workspace retention (doc §10.2, Phase 7) --------------------------

    async def archive_workspace(self, workspace_id: str, *, archiver_image: str) -> bytes:
        """No already-running sandbox to `exec` into by the time retention reaps an
        idle workspace (its sandbox, if any, is always destroyed first) — so this
        mounts the named volume read-only into a short-lived throwaway container of
        its own, tars it via `exec` (never `get_archive`/`docker cp` — confirmed
        unreliable against these mounts back in Phase 1, see docker.py's module
        docstring lesson), and removes the container regardless of outcome."""
        data, exit_code = await self._exec_against_workspace_mount(
            workspace_id, archiver_image, lambda mount_path: ["tar", "czf", "-", "-C", mount_path, "."]
        )
        if exit_code:
            raise ProvisionerError(f"failed to archive workspace {workspace_id}: tar exited {exit_code}")
        return data

    async def measure_workspace_usage(self, workspace_id: str, *, archiver_image: str) -> int:
        """Backs `WorkspaceService.check_quota()` — without something periodically
        calling this and writing the result into `Workspace.used_mb` (the
        reconciler's job), quota checks run against a stale value forever. Same
        throwaway-mount pattern as `archive_workspace`, just `du` instead of `tar`."""
        data, exit_code = await self._exec_against_workspace_mount(
            workspace_id, archiver_image, lambda mount_path: ["du", "-sm", mount_path]
        )
        if exit_code:
            raise ProvisionerError(f"failed to measure workspace {workspace_id} usage: du exited {exit_code}")
        return int(data.split()[0])

    async def _exec_against_workspace_mount(
        self, workspace_id: str, archiver_image: str, build_cmd: Callable[[str], list[str]]
    ) -> tuple[bytes, int]:
        """Shared plumbing for `archive_workspace`/`measure_workspace_usage`: mounts
        the named volume read-only into a short-lived throwaway container, execs
        `build_cmd(mount_path)`, and always removes the container afterward."""
        mount_path = "/workspace-src"
        container = await self._docker.containers.run(
            {
                "Image": archiver_image,
                "Cmd": ["sleep", "infinity"],
                "HostConfig": {
                    "Mounts": [
                        {
                            "Type": "volume",
                            "Source": self._workspace_volume_name(workspace_id),
                            "Target": mount_path,
                            "ReadOnly": True,
                        }
                    ],
                    "NetworkMode": "none",
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges"],
                },
            },
            name=f"kubesandbox-wsmount-{uuid.uuid4().hex[:12]}",
        )
        try:
            await self._wait_container_running(container)
            exec_obj = await container.exec(cmd=build_cmd(mount_path), stdout=True, stderr=False, user="0:0")
            chunks: list[bytes] = []
            async with exec_obj.start(detach=False) as stream:
                while (msg := await stream.read_out()) is not None:
                    chunks.append(msg.data)
            info = await exec_obj.inspect()
            return b"".join(chunks), info.get("ExitCode") or 0
        except DockerError as exc:
            raise ProvisionerError(f"failed to run against workspace {workspace_id} mount: {exc}") from exc
        finally:
            await self._force_remove(container.id)

    async def restore_workspace(self, workspace_id: str, data: bytes, *, archiver_image: str) -> None:
        """Symmetric to `archive_workspace()`: untars `data` onto a fresh named volume
        (created if missing, same idempotent `volumes.create()` `acquire()` uses),
        via a throwaway container mounting it writable this time, then fixes
        ownership exactly like a brand-new persistent sandbox's first `acquire()`
        would. Used to un-archive a workspace back to `active` (doc §10.2)."""
        volume_name = self._workspace_volume_name(workspace_id)
        await self._docker.volumes.create({"Name": volume_name})
        mount_path = "/workspace-restore"
        container = await self._docker.containers.run(
            {
                "Image": archiver_image,
                "Cmd": ["sleep", "infinity"],
                "HostConfig": {
                    "Mounts": [
                        {"Type": "volume", "Source": volume_name, "Target": mount_path, "ReadOnly": False}
                    ],
                    "NetworkMode": "none",
                    "CapDrop": ["ALL"],
                    "CapAdd": ["CHOWN"],
                    "SecurityOpt": ["no-new-privileges"],
                },
            },
            name=f"kubesandbox-wsrestore-{uuid.uuid4().hex[:12]}",
        )
        try:
            await self._wait_container_running(container)
            exec_obj = await container.exec(
                cmd=["tar", "xzf", "-", "-C", mount_path], stdin=True, stdout=True, stderr=True, user="0:0"
            )
            stream = exec_obj.start(detach=False)
            output = bytearray()
            try:
                await stream._init()  # noqa: SLF001 — must run before write_in/half-close, see module docstring
                await stream.write_in(data)
                _half_close_stdin(stream)
                while (msg := await stream.read_out()) is not None:
                    output.extend(msg.data)
            finally:
                with contextlib.suppress(Exception):
                    await stream.close()
            info = await exec_obj.inspect()
            if info.get("ExitCode"):
                raise ProvisionerError(
                    f"failed to restore workspace {workspace_id} (exit {info.get('ExitCode')}): "
                    f"{output.decode(errors='replace')!r}"
                )
            await self._fix_workspace_ownership(container, mount_path)
        except DockerError as exc:
            raise ProvisionerError(f"failed to restore workspace {workspace_id}: {exc}") from exc
        finally:
            await self._force_remove(container.id)

    async def delete_workspace_volume(self, workspace_id: str) -> None:
        volume = DockerVolume(self._docker, self._workspace_volume_name(workspace_id))
        try:
            await volume.delete(force=True)
        except DockerError as exc:
            if exc.status != 404:
                raise ProvisionerError(f"failed to delete workspace volume for {workspace_id}: {exc}") from exc

    # -- orphan GC (doc §4.1, Phase 7) -------------------------------------------------

    async def list_sandbox_refs(self) -> list[NativeSandboxRef]:
        containers = await self._docker.containers.list(
            all=True, filters=clean_filters({"label": ["io.kubesandbox.sandbox-id"]})
        )
        mains: dict[str, dict] = {}
        sidecar_refs: dict[str, dict[str, str]] = {}
        for container in containers:
            info = await container.show()
            labels = (info.get("Config") or {}).get("Labels") or {}
            sandbox_id = labels.get("io.kubesandbox.sandbox-id")
            if not sandbox_id:
                continue
            sidecar_name = labels.get("io.kubesandbox.sidecar")
            if sidecar_name:
                sidecar_refs.setdefault(sandbox_id, {})[sidecar_name] = container.id
            else:
                mains[sandbox_id] = info

        return [
            NativeSandboxRef(
                sandbox_id=sandbox_id,
                native_ref=info["Id"],
                created_at=datetime.fromisoformat(info["Created"].replace("Z", "+00:00")),
                sidecar_refs=sidecar_refs.get(sandbox_id, {}),
            )
            for sandbox_id, info in mains.items()
        ]

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
        container = self._docker.containers.container(handle.native_ref)

        if command.files:
            await self.put_files(handle, command.files)

        raw = await self._exec_with_stdin(
            container,
            command.command,
            stdin=command.stdin.encode(),
            timeout_seconds=command.timeout_seconds,
            max_output_bytes=command.max_output_bytes,
            user=_SANDBOX_EXEC_USER,
            workdir="/workspace",
            sandbox_id=handle.sandbox_id,
        )

        variables = await self._read_variable_dump(container) if command.capture_variables else None

        return BatchRunResult(
            run_id=str(uuid.uuid4()),
            exit_code=raw.exit_code,
            stdout=raw.stdout.decode(errors="replace"),
            stderr=raw.stderr.decode(errors="replace"),
            duration_ms=raw.duration_ms,
            truncated=raw.truncated,
            timed_out=raw.timed_out,
            variables=variables,
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
        container = self._docker.containers.container(self._resolve_target(handle, target))
        # Only "main" is forced into the sandbox's restricted uid — a sidecar is a
        # vetted database image with its own expected user, not sandboxed user code
        # (see _create_sidecar's docstring).
        user = _SANDBOX_EXEC_USER if target == "main" else ""
        workdir = "/workspace" if target == "main" else None

        raw = await self._exec_with_stdin(
            container,
            command,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            user=user,
            workdir=workdir,
            sandbox_id=handle.sandbox_id,
        )
        return BatchRunResult(
            run_id=str(uuid.uuid4()),
            exit_code=raw.exit_code,
            stdout=raw.stdout.decode(errors="replace"),
            stderr=raw.stderr.decode(errors="replace"),
            duration_ms=raw.duration_ms,
            truncated=raw.truncated,
            timed_out=raw.timed_out,
        )

    def _resolve_target(self, handle: SandboxHandle, target: str) -> str:
        if target == "main":
            return handle.native_ref
        try:
            return handle.sidecar_refs[target]
        except KeyError:
            raise ProvisionerError(f"sandbox {handle.sandbox_id} has no sidecar named {target!r}") from None

    async def _exec_with_stdin(
        self,
        container: aiodocker.docker.DockerContainer,
        cmd: list[str],
        *,
        stdin: bytes,
        timeout_seconds: int,
        max_output_bytes: int,
        user: str,
        workdir: str | None,
        sandbox_id: str,
    ) -> _RawExecResult:
        """Runs `cmd` to completion (or wall-clock timeout), writing `stdin` up front
        then signaling EOF (see module docstring's `_half_close_stdin` explanation).
        Shared by exec_batch (always the sandbox's own restricted uid, against main)
        and exec_in (any target/user the caller resolves) — the only difference
        between the two call sites is which container/user/workdir gets exec'd into.
        """
        try:
            exec_obj = await container.exec(
                cmd=cmd,
                stdin=True,
                stdout=True,
                stderr=True,
                tty=False,
                workdir=workdir,
                user=user,
            )
        except DockerError as exc:
            if exc.status == 404:
                raise SandboxNotFoundError(sandbox_id) from exc
            raise ProvisionerError(f"failed to start exec: {exc}") from exc

        stream = exec_obj.start(detach=False)
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        truncated = False
        timed_out = False
        start = time.monotonic()

        async def _pump() -> None:
            nonlocal truncated
            await stream._init()  # noqa: SLF001 — must run before write_in/half-close
            if stdin:
                await stream.write_in(stdin)
            _half_close_stdin(stream)  # signal stdin EOF without closing the read side

            while True:
                msg = await stream.read_out()
                if msg is None:
                    return
                buf = stderr_buf if msg.stream == 2 else stdout_buf
                if len(buf) < max_output_bytes:
                    buf.extend(msg.data)
                else:
                    truncated = True  # keep draining so we still learn the real exit code

        try:
            await asyncio.wait_for(_pump(), timeout=timeout_seconds)
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

        return _RawExecResult(
            exit_code=exit_code,
            stdout=bytes(stdout_buf),
            stderr=bytes(stderr_buf),
            duration_ms=duration_ms,
            truncated=truncated,
            timed_out=timed_out,
        )

    async def _exec_capture(
        self,
        container: aiodocker.docker.DockerContainer,
        cmd: list[str],
        *,
        user: str | None = _SANDBOX_EXEC_USER,
    ) -> tuple[int, bytes]:
        """Run `cmd` via `exec` and capture its combined stdout+stderr as raw bytes —
        shared by variable-dump reading, file download, tree listing (all default to
        the sandbox's own restricted uid, reading from main's /workspace) and sidecar
        healthcheck polling (`user=None` — a sidecar has no sandbox uid to run as).
        Reads via `exec` (`cat`/`find`) rather than the archive/`docker cp` API:
        confirmed against a live daemon that `get_archive` can't find a file written
        into a tmpfs mount on a ReadonlyRootfs container (the same archive-endpoint
        limitation already worked around in `put_files`), even though the file
        demonstrably exists from the writer's own point of view."""
        exec_obj = await container.exec(cmd=cmd, stdout=True, stderr=True, user=user or "")
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
