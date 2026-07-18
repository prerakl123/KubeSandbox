"""Kubernetes-backed Provisioner for the `aks-prod` environment, kind-cluster-testable
against `local` too (doc §4.2, §7, roadmap Phase 3).

Same interface contract as `DockerProvisioner`: a sandbox is a single already-running
Pod ("sleep infinity") that batch commands are `exec`'d into. Unlike Docker, isolation
is expressed as a **namespace-per-sandbox** (doc §6 Layer 3): `acquire()` creates a
fresh Namespace holding a default-deny NetworkPolicy, a ResourceQuota, a LimitRange, and
the sandbox Pod itself; `destroy()` deletes the whole namespace so nothing can leak.

Notable implementation detail — stdin EOF over the K8s exec WebSocket protocol:
unlike Docker's raw hijacked TCP stream (where a TCP half-close cleanly signals "no
more input" while the read side stays open, see docker.py's `_half_close_stdin`), the
K8s exec channel multiplexes stdin/stdout/stderr/exit-status as byte-prefixed frames
over ONE WebSocket connection — there is no independent half-close per channel in the
"v4.channel.k8s.io" subprotocol that `kubernetes_asyncio`'s `WsApiClient` requests by
default. Confirmed empirically against a live kind cluster: with only v4 negotiated, a
process blocked on stdin (e.g. `cat`, or a batch runner's `input()`) never sees EOF and
the exec session hangs until our own wall-clock timeout reaps it. The fix, also
confirmed live: offer "v5.channel.k8s.io" alongside v4 in the WebSocket handshake
(`_MultiProtocolWsApiClient` below) and send a control frame — byte 255 (close-channel
index) followed by the target channel index (0 = stdin) — which closes just that
stream and delivers real EOF to the remote process while stdout/stderr/exit-status
keep flowing on the same connection. If a server only supports v4 (older clusters),
this frame is silently ignored — degrades safely to the wall-clock timeout, same
fallback philosophy as Docker's `_half_close_stdin`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from datetime import UTC, datetime

from aiohttp import WSMsgType
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.stream import WsApiClient
from kubernetes_asyncio.stream.ws_client import ERROR_CHANNEL, STDERR_CHANNEL, STDIN_CHANNEL, STDOUT_CHANNEL

from app.core.errors import ProvisionerError, SandboxNotFoundError
from app.core.logging import get_logger
from app.domain.execution import (
    VARIABLE_DUMP_PATH,
    BatchCommand,
    BatchRunResult,
    SandboxHandle,
    SandboxSpec,
    SandboxState,
    SandboxStatus,
)
from app.provisioners.base import PTYStream

logger = get_logger(__name__)

_POD_NAME = "sandbox"
_MAIN_CONTAINER = "main"
# Fixed across every golden image (doc §6 Layer 1: runAsUser/Group: 10001, always).
_SANDBOX_UID = 10001
_TERMINATION_GRACE_SECONDS = 5
_POD_READY_TIMEOUT_SECONDS = 60
_FILE_OP_TIMEOUT_SECONDS = 30
_CLOSE_CHANNEL_INDEX = 255  # v5.channel.k8s.io: closes one stream without ending the connection
_STDIN_CHUNK_BYTES = 512 * 1024


class _MultiProtocolWsApiClient(WsApiClient):
    """Offers v5 (preferred) and v4 (fallback) subprotocols — see module docstring.
    Upstream `WsApiClient.request()` hardcodes v4 only when the caller doesn't set
    the header itself, which is why this override exists."""

    async def request(self, method, url, query_params=None, headers=None, **kwargs):
        headers = dict(headers or {})
        headers["sec-websocket-protocol"] = "v5.channel.k8s.io, v4.channel.k8s.io"
        return await super().request(method, url, query_params=query_params, headers=headers, **kwargs)


def _volume_name(path: str) -> str:
    slug = path.strip("/").replace("/", "-") or "root"
    return f"vol-{slug}"


class KubernetesProvisioner:
    def __init__(
        self,
        configuration: client.Configuration,
        *,
        namespace_prefix: str = "kubesandbox-sb-",
        runtime_class: str | None = None,
    ) -> None:
        self._api_client = client.ApiClient(configuration)
        self._core_v1 = client.CoreV1Api(self._api_client)
        self._networking_v1 = client.NetworkingV1Api(self._api_client)
        # A second, WebSocket-flavored client dedicated to exec — see module docstring.
        self._ws_api_client = _MultiProtocolWsApiClient(configuration)
        self._exec_v1 = client.CoreV1Api(self._ws_api_client)
        self._namespace_prefix = namespace_prefix
        self._runtime_class = runtime_class

    @classmethod
    async def create(
        cls,
        *,
        kubeconfig_path: str | None = None,
        namespace_prefix: str = "kubesandbox-sb-",
        runtime_class: str | None = None,
    ) -> KubernetesProvisioner:
        configuration = client.Configuration()
        if kubeconfig_path:
            await config.load_kube_config(config_file=kubeconfig_path, client_configuration=configuration)
        elif os.environ.get("KUBERNETES_SERVICE_HOST"):
            # In-cluster (aks-prod control-plane pod) — sync call, no kubeconfig file involved.
            config.load_incluster_config(client_configuration=configuration)
        else:
            await config.load_kube_config(client_configuration=configuration)
        return cls(configuration, namespace_prefix=namespace_prefix, runtime_class=runtime_class)

    async def aclose(self) -> None:
        await self._api_client.close()
        await self._ws_api_client.close()

    # -- lifecycle -------------------------------------------------------------------

    async def acquire(self, spec: SandboxSpec) -> SandboxHandle:
        sandbox_id = str(uuid.uuid4())
        namespace = f"{self._namespace_prefix}{sandbox_id.replace('-', '')[:16]}"

        try:
            await self._core_v1.create_namespace(
                client.V1Namespace(
                    metadata=client.V1ObjectMeta(
                        name=namespace,
                        labels={"io.kubesandbox.sandbox-id": sandbox_id},
                        annotations=dict(spec.labels),
                    )
                )
            )
            await self._create_network_policy(namespace)
            await self._create_resource_quota(namespace, spec)
            await self._create_limit_range(namespace, spec)
            await self._create_pod(namespace, sandbox_id, spec)
            await self._wait_ready(namespace)
        except ApiException as exc:
            await self._cleanup_namespace(namespace)
            raise ProvisionerError(f"failed to provision sandbox namespace {namespace!r}: {exc}") from exc
        except Exception:
            await self._cleanup_namespace(namespace)
            raise

        return SandboxHandle(
            sandbox_id=sandbox_id,
            backend="kubernetes",
            native_ref=namespace,
            created_at=datetime.now(UTC),
        )

    async def _create_network_policy(self, namespace: str) -> None:
        # Default-deny both directions (doc §6 Layer 3, §12) — no ingress/egress rules
        # means nothing is allowed; the deploy overlay owns any allowlist, never this code.
        policy = client.V1NetworkPolicy(
            metadata=client.V1ObjectMeta(name="default-deny", namespace=namespace),
            spec=client.V1NetworkPolicySpec(
                pod_selector=client.V1LabelSelector(),
                policy_types=["Ingress", "Egress"],
                ingress=[],
                egress=[],
            ),
        )
        await self._networking_v1.create_namespaced_network_policy(namespace, policy)

    async def _create_resource_quota(self, namespace: str, spec: SandboxSpec) -> None:
        hard = {
            "pods": "1",
            "requests.cpu": spec.resources.cpu,
            "requests.memory": spec.resources.memory,
            "limits.cpu": spec.resources.cpu,
            "limits.memory": spec.resources.memory,
        }
        if spec.resources.ephemeral_storage_mb is not None:
            value = f"{spec.resources.ephemeral_storage_mb}Mi"
            hard["requests.ephemeral-storage"] = value
            hard["limits.ephemeral-storage"] = value
        quota = client.V1ResourceQuota(
            metadata=client.V1ObjectMeta(name="sandbox-quota", namespace=namespace),
            spec=client.V1ResourceQuotaSpec(hard=hard),
        )
        await self._core_v1.create_namespaced_resource_quota(namespace, quota)

    async def _create_limit_range(self, namespace: str, spec: SandboxSpec) -> None:
        # Defaults for any future sidecar that omits its own resources (Phase 5) — the
        # sandbox's own main container always sets resources explicitly regardless.
        limits = {"cpu": spec.resources.cpu, "memory": spec.resources.memory}
        limit_range = client.V1LimitRange(
            metadata=client.V1ObjectMeta(name="sandbox-limits", namespace=namespace),
            spec=client.V1LimitRangeSpec(
                limits=[
                    client.V1LimitRangeItem(
                        type="Container", default=limits, default_request=limits, max=limits
                    )
                ]
            ),
        )
        await self._core_v1.create_namespaced_limit_range(namespace, limit_range)

    async def _create_pod(self, namespace: str, sandbox_id: str, spec: SandboxSpec) -> None:
        resource_requests = {"cpu": spec.resources.cpu, "memory": spec.resources.memory}
        if spec.resources.ephemeral_storage_mb is not None:
            resource_requests["ephemeral-storage"] = f"{spec.resources.ephemeral_storage_mb}Mi"

        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=_POD_NAME,
                namespace=namespace,
                # spec.labels (e.g. "io.kubesandbox.component": "python@3.12.4") carries
                # free-form component-ref strings that fail Kubernetes' label-value regex
                # (no "@" or "/") -- confirmed live against a real API server, which
                # rejects them with a 422. Docker has no such restriction on its labels.
                # Annotations have no such charset restriction, so descriptive values
                # belong there; only the UUID is a real (safe) label.
                labels={"io.kubesandbox.sandbox-id": sandbox_id},
                annotations=dict(spec.labels),
            ),
            spec=client.V1PodSpec(
                restart_policy="Never",
                automount_service_account_token=False,
                termination_grace_period_seconds=_TERMINATION_GRACE_SECONDS,
                runtime_class_name=self._runtime_class,
                security_context=client.V1PodSecurityContext(
                    run_as_non_root=True,
                    run_as_user=_SANDBOX_UID,
                    run_as_group=_SANDBOX_UID,
                    # Grants the sandbox uid write access to root-owned emptyDir volumes
                    # (K8s applies fsGroup as a supplemental group + adjusts volume perms) —
                    # the Kubernetes-native equivalent of Docker's explicit tmpfs uid/gid/mode.
                    fs_group=_SANDBOX_UID,
                    seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                ),
                containers=[
                    client.V1Container(
                        name=_MAIN_CONTAINER,
                        image=spec.image,
                        command=list(spec.command),
                        working_dir=spec.workdir,
                        env=[client.V1EnvVar(name=k, value=v) for k, v in spec.env.items()],
                        resources=client.V1ResourceRequirements(
                            requests=resource_requests, limits=dict(resource_requests)
                        ),
                        security_context=client.V1SecurityContext(
                            allow_privilege_escalation=False,
                            capabilities=client.V1Capabilities(drop=["ALL"]),
                            read_only_root_filesystem=spec.read_only_root_filesystem,
                        ),
                        volume_mounts=[
                            client.V1VolumeMount(name=_volume_name(p), mount_path=p)
                            for p in spec.writable_paths
                        ],
                    )
                ],
                # Disk-backed (not `medium: Memory`) emptyDir on purpose: a Memory-backed
                # emptyDir is tmpfs under the hood, and Docker's own tmpfs mounts needed an
                # explicit `exec` mount option to let a compiled language's `go run`-style
                # execve out of /tmp work at all (see docs/TASK_CHECKLIST.md Phase 1/2's
                # "5th bug"). Disk-backed emptyDir has no such noexec default, sidestepping
                # that entire class of bug instead of reproducing it.
                volumes=[
                    client.V1Volume(name=_volume_name(p), empty_dir=client.V1EmptyDirVolumeSource())
                    for p in spec.writable_paths
                ],
            ),
        )
        await self._core_v1.create_namespaced_pod(namespace, pod)

    async def _wait_ready(self, namespace: str) -> None:
        deadline = time.monotonic() + _POD_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            pod = await self._core_v1.read_namespaced_pod(_POD_NAME, namespace)
            phase = pod.status.phase
            statuses = pod.status.container_statuses or []
            if phase == "Running" and statuses and all(s.ready for s in statuses):
                return
            if phase == "Failed":
                raise ProvisionerError(f"sandbox pod in {namespace!r} failed to start: {pod.status.reason}")
            await asyncio.sleep(0.5)
        raise ProvisionerError(f"timed out waiting for sandbox pod in {namespace!r} to become ready")

    async def _cleanup_namespace(self, namespace: str) -> None:
        with contextlib.suppress(ApiException):
            await self._core_v1.delete_namespace(namespace)

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        try:
            pod = await self._core_v1.read_namespaced_pod(_POD_NAME, handle.native_ref)
        except ApiException as exc:
            if exc.status == 404:
                return SandboxStatus(sandbox_id=handle.sandbox_id, state=SandboxState.TERMINATED)
            raise ProvisionerError(f"failed to read status for {handle.sandbox_id}: {exc}") from exc

        phase = pod.status.phase
        mapped = {
            "Pending": SandboxState.PROVISIONING,
            "Running": SandboxState.ACTIVE,
        }.get(phase, SandboxState.TERMINATED)
        return SandboxStatus(sandbox_id=handle.sandbox_id, state=mapped, detail=phase)

    async def recycle(self, handle: SandboxHandle) -> None:
        """Wipe /workspace and confirm the pod is still healthy (doc §4.3). Not wired
        to a real PoolManager yet (Phase 7) — SandboxService always destroys ephemeral
        sandboxes today instead of recycling them."""
        await self._run_exec(
            handle.native_ref,
            ["sh", "-c", "rm -rf /workspace/* /workspace/.[!.]* 2>/dev/null; true"],
            stdin=b"",
            timeout_seconds=_FILE_OP_TIMEOUT_SECONDS,
            max_output_bytes=1_000_000,
        )

    async def destroy(self, handle: SandboxHandle) -> None:
        """Graceful eradication: deleting the namespace cascades to the pod (and its
        NetworkPolicy/ResourceQuota/LimitRange) — nothing sandbox-scoped can leak once
        this call succeeds. Safe to call more than once: "already gone" is success."""
        try:
            await self._core_v1.delete_namespace(handle.native_ref)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise ProvisionerError(f"failed to destroy sandbox {handle.sandbox_id}: {exc}") from exc

    # -- files -------------------------------------------------------------------------

    async def put_files(self, handle: SandboxHandle, files: dict[str, str]) -> None:
        if not files:
            return
        for rel_path, content in files.items():
            result = await self._run_exec(
                handle.native_ref,
                ["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "--", rel_path],
                stdin=content.encode("utf-8"),
                timeout_seconds=_FILE_OP_TIMEOUT_SECONDS,
                max_output_bytes=1_000_000,
            )
            if result.exit_code:
                raise ProvisionerError(
                    f"failed to write file {rel_path!r} into sandbox "
                    f"(exit {result.exit_code}): {result.stderr or result.stdout!r}"
                )

    # -- batch execution ----------------------------------------------------------------

    async def exec_batch(self, handle: SandboxHandle, command: BatchCommand) -> BatchRunResult:
        if command.files:
            await self.put_files(handle, command.files)

        try:
            result = await self._run_exec(
                handle.native_ref,
                command.command,
                stdin=command.stdin.encode("utf-8"),
                timeout_seconds=command.timeout_seconds,
                max_output_bytes=command.max_output_bytes,
            )
        except SandboxNotFoundError:
            raise
        except ApiException as exc:
            raise ProvisionerError(f"failed to start batch exec: {exc}") from exc

        variables = None
        if command.capture_variables:
            variables = await self._read_variable_dump(handle.native_ref)

        return BatchRunResult(
            run_id=str(uuid.uuid4()),
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            truncated=result.truncated,
            timed_out=result.timed_out,
            variables=variables,
        )

    async def _read_variable_dump(self, namespace: str) -> dict | None:
        try:
            result = await self._run_exec(
                namespace,
                ["cat", VARIABLE_DUMP_PATH],
                stdin=b"",
                timeout_seconds=_FILE_OP_TIMEOUT_SECONDS,
                max_output_bytes=5_000_000,
            )
        except (ApiException, SandboxNotFoundError) as exc:
            logger.warning("variable_dump_read_failed", stage="exec_create", error=str(exc))
            return None

        if result.exit_code:
            logger.warning("variable_dump_read_failed", stage="cat_exit", exit_code=result.exit_code)
            return None

        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            logger.warning("variable_dump_read_failed", stage="parse", error=str(exc))
            return None

    # -- low-level exec plumbing ---------------------------------------------------------

    async def _run_exec(
        self,
        namespace: str,
        command: list[str],
        *,
        stdin: bytes,
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> BatchRunResult:
        try:
            ws_ctx = await self._exec_v1.connect_get_namespaced_pod_exec(
                _POD_NAME,
                namespace,
                container=_MAIN_CONTAINER,
                command=command,
                stderr=True,
                stdin=True,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
        except ApiException as exc:
            if exc.status == 404:
                raise SandboxNotFoundError(namespace) from exc
            raise

        stdout_buf = bytearray()
        stderr_buf = bytearray()
        truncated = False
        timed_out = False
        exit_frame: bytes | None = None
        start = time.monotonic()

        async with ws_ctx as ws:
            offset = 0
            while True:
                chunk = stdin[offset : offset + _STDIN_CHUNK_BYTES]
                await ws.send_bytes(bytes([STDIN_CHANNEL]) + chunk)
                offset += _STDIN_CHUNK_BYTES
                if offset >= len(stdin):
                    break
            # Closes just the stdin stream so a blocking read sees real EOF while we
            # keep reading stdout/stderr/exit-status on this same connection — see
            # module docstring for why this specific byte sequence is needed.
            await ws.send_bytes(bytes([_CLOSE_CHANNEL_INDEX, STDIN_CHANNEL]))

            try:
                async with asyncio.timeout(timeout_seconds):
                    async for msg in ws:
                        if msg.type not in (WSMsgType.BINARY, WSMsgType.TEXT):
                            continue
                        data = msg.data if isinstance(msg.data, (bytes, bytearray)) else msg.data.encode()
                        if not data:
                            continue
                        channel, payload = data[0], data[1:]
                        if channel in (STDOUT_CHANNEL, STDERR_CHANNEL):
                            buf = stdout_buf if channel == STDOUT_CHANNEL else stderr_buf
                            if len(buf) < max_output_bytes:
                                buf.extend(payload)
                            else:
                                truncated = True  # keep draining so we still learn the real exit code
                        elif channel == ERROR_CHANNEL:
                            exit_frame = bytes(payload)
                            break
            except TimeoutError:
                timed_out = True

        duration_ms = int((time.monotonic() - start) * 1000)

        if timed_out:
            exit_code = 124  # mirrors coreutils' `timeout` convention, matches DockerProvisioner
        elif exit_frame is not None:
            exit_code = WsApiClient.parse_error_data(exit_frame)
        else:
            raise ProvisionerError(f"exec session in {namespace!r} closed with no exit status")

        return BatchRunResult(
            run_id=str(uuid.uuid4()),
            exit_code=exit_code,
            stdout=stdout_buf.decode(errors="replace"),
            stderr=stderr_buf.decode(errors="replace"),
            duration_ms=duration_ms,
            truncated=truncated,
            timed_out=timed_out,
        )

    # -- interactive (Phase 4) -----------------------------------------------------------

    async def attach(self, handle: SandboxHandle) -> PTYStream:
        raise NotImplementedError(
            "Interactive PTY attach is Phase 4 (doc roadmap §20); KubernetesProvisioner "
            "currently only supports batch execution (doc §5.1)."
        )
