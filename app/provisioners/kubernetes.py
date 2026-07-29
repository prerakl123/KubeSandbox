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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from aiohttp import WSMsgType
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.stream import WsApiClient
from kubernetes_asyncio.stream.ws_client import (
    ERROR_CHANNEL,
    RESIZE_CHANNEL,
    STDERR_CHANNEL,
    STDIN_CHANNEL,
    STDOUT_CHANNEL,
)

from app.core.errors import ProvisionerError, SandboxNotFoundError
from app.core.logging import get_logger
from app.domain.execution import (
    VARIABLE_DUMP_PATH,
    BatchCommand,
    BatchRunResult,
    FileEntry,
    NativeSandboxRef,
    ResourceSpec,
    SandboxHandle,
    SandboxSpec,
    SandboxState,
    SandboxStatus,
    SidecarSpec,
)
from app.provisioners.base import PTYEvent, PTYStream, parse_find_output
from app.provisioners.resources import (
    format_bytes_to_memory,
    format_nanocpus_to_cpu,
    parse_cpu_to_nanocpus,
    parse_memory_to_bytes,
)

logger = get_logger(__name__)

_POD_NAME = "sandbox"
_MAIN_CONTAINER = "main"
_WORKSPACE_PVC_NAME = "workspace-pvc"
_DEFAULT_WORKSPACE_SIZE_MB = 1024
# Fixed across every golden image (doc §6 Layer 1: runAsUser/Group: 10001, always).
_SANDBOX_UID = 10001
_TERMINATION_GRACE_SECONDS = 5
_POD_READY_TIMEOUT_SECONDS = 60
_FILE_OP_TIMEOUT_SECONDS = 30
_CLOSE_CHANNEL_INDEX = 255  # v5.channel.k8s.io: closes one stream without ending the connection
_STDIN_CHUNK_BYTES = 512 * 1024
# `-A`: attach to this session if it already exists, else create it — a single command
# gives us create-or-reattach for free (doc §20 Phase 4 "reattach-after-disconnect").
# Requires `dtach` baked into the golden image (components/*/Dockerfile) — swapped in
# for tmux, which doesn't work in these containers at all (confirmed live: fatal
# cgroup-via-systemd-dbus failure with no dbus available); see docker.py's copy of
# this constant for the full explanation.
_INTERACTIVE_SHELL_CMD = ["dtach", "-A", "/tmp/.kubesandbox-attach.sock", "-e", "none", "-z", "/bin/bash"]


class _MultiProtocolWsApiClient(WsApiClient):
    """Offers v5 (preferred) and v4 (fallback) subprotocols — see module docstring.
    Upstream `WsApiClient.request()` hardcodes v4 only when the caller doesn't set
    the header itself, which is why this override exists."""

    async def request(self, method, url, query_params=None, headers=None, **kwargs):
        headers = dict(headers or {})
        headers["sec-websocket-protocol"] = "v5.channel.k8s.io, v4.channel.k8s.io"
        return await super().request(method, url, query_params=query_params, headers=headers, **kwargs)


@dataclass(frozen=True)
class _RawExecResult:
    """Same shape as BatchRunResult minus the fields only exec_batch's caller needs
    (run_id) and with stdout/stderr as raw bytes rather than lossily-decoded str."""

    exit_code: int
    stdout: bytes
    stderr: bytes
    duration_ms: int
    truncated: bool
    timed_out: bool


def _volume_name(path: str) -> str:
    slug = path.strip("/").replace("/", "-") or "root"
    return f"vol-{slug}"


def _sidecar_volume_name(sidecar_name: str, path: str) -> str:
    """Namespaced separately from _volume_name so a sidecar's writable path can never
    collide with main's (or another sidecar's) volume name, even if two components
    happen to declare the same path string."""
    slug = path.strip("/").replace("/", "-") or "root"
    return f"vol-{sidecar_name}-{slug}"


def _total_resources(spec: SandboxSpec) -> tuple[str, str]:
    """Sum of main + every sidecar's resource request, in k8s quantity strings —
    what the namespace's ResourceQuota must actually allow (doc §20 Phase 5): each
    sidecar adds its own request on top of main's, so a quota sized to main alone
    would reject pod admission the moment a sidecar is attached."""
    cpu_nanos = parse_cpu_to_nanocpus(spec.resources.cpu) + sum(
        parse_cpu_to_nanocpus(s.resources.cpu) for s in spec.sidecars
    )
    memory_bytes = parse_memory_to_bytes(spec.resources.memory) + sum(
        parse_memory_to_bytes(s.resources.memory) for s in spec.sidecars
    )
    return format_nanocpus_to_cpu(cpu_nanos), format_bytes_to_memory(memory_bytes)


def _max_single_container_resources(spec: SandboxSpec) -> tuple[str, str]:
    """Largest single container's cpu/memory request across main + sidecars — the
    LimitRange's `max` must allow this or a sidecar requesting more than main alone
    would fail admission, even though the *namespace total* (see _total_resources) is
    still within quota."""
    cpu_nanos = max(
        [parse_cpu_to_nanocpus(spec.resources.cpu)]
        + [parse_cpu_to_nanocpus(s.resources.cpu) for s in spec.sidecars]
    )
    memory_bytes = max(
        [parse_memory_to_bytes(spec.resources.memory)]
        + [parse_memory_to_bytes(s.resources.memory) for s in spec.sidecars]
    )
    return format_nanocpus_to_cpu(cpu_nanos), format_bytes_to_memory(memory_bytes)


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
        persistent = spec.workspace_id is not None
        # A persistent workspace's PVC is namespace-scoped and Kubernetes has no
        # cross-namespace PVC mount — so unlike the ephemeral path (a fresh,
        # randomly-named namespace every time, deleted whole on destroy()), a
        # persistent sandbox reuses one namespace, deterministically named off the
        # workspace id, for that workspace's entire lifetime (doc §10.2, Phase 7).
        namespace = (
            f"{self._namespace_prefix}ws-{spec.workspace_id}"
            if persistent
            else f"{self._namespace_prefix}{sandbox_id.replace('-', '')[:16]}"
        )

        try:
            if persistent:
                namespace_existed = await self._ensure_persistent_namespace(namespace, spec)
                if namespace_existed:
                    # Only a *reused* namespace can possibly hold a leftover pod from a
                    # prior, not-cleanly-destroyed session — one we just created this
                    # instant cannot.
                    await self._ensure_no_stale_pod(namespace)
            else:
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
            await self._cleanup_after_acquire_failure(namespace, persistent=persistent)
            raise ProvisionerError(f"failed to provision sandbox namespace {namespace!r}: {exc}") from exc
        except Exception:
            await self._cleanup_after_acquire_failure(namespace, persistent=persistent)
            raise

        return SandboxHandle(
            sandbox_id=sandbox_id,
            backend="kubernetes",
            native_ref=namespace,
            created_at=datetime.now(UTC),
            # A sidecar's "native ref" is just its own container name — containers in
            # one pod are already addressed by name, unlike Docker where each sidecar
            # is a genuinely separate container with its own id.
            sidecar_refs={s.name: s.name for s in spec.sidecars},
            persistent=persistent,
        )

    async def _ensure_persistent_namespace(self, namespace: str, spec: SandboxSpec) -> bool:
        """Provisions the namespace + its NetworkPolicy/ResourceQuota/LimitRange/PVC
        exactly once per workspace — a no-op on every subsequent acquire() for the
        same workspace, detected via a plain existence check rather than tracking
        "did I already do this" anywhere else (the cluster's own state is the only
        source of truth `acquire()` needs, consistent with "any replica can serve any
        session", doc §2). Returns whether the namespace already existed (reused) —
        `acquire()` only bothers checking for a leftover stale pod in that case; a
        namespace created this instant cannot possibly have one."""
        try:
            await self._core_v1.read_namespace(namespace)
            return True
        except ApiException as exc:
            if exc.status != 404:
                raise

        await self._core_v1.create_namespace(
            client.V1Namespace(
                metadata=client.V1ObjectMeta(
                    name=namespace,
                    labels={"io.kubesandbox.workspace-id": spec.workspace_id},
                    annotations={**spec.labels, "io.kubesandbox.workspace-id": spec.workspace_id},
                )
            )
        )
        await self._create_network_policy(namespace)
        await self._create_resource_quota(namespace, spec)
        await self._create_limit_range(namespace, spec)
        await self._create_pvc(namespace, spec)
        return False

    async def _create_pvc(self, namespace: str, spec: SandboxSpec) -> None:
        # No storageClassName set — uses the cluster's default StorageClass. A real
        # aks-prod deployment should pin one explicitly (e.g. Azure Disk/Files backed);
        # left unset here since neither this session nor kind has one worth hardcoding.
        size_mb = spec.workspace_size_mb or _DEFAULT_WORKSPACE_SIZE_MB
        pvc = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=_WORKSPACE_PVC_NAME, namespace=namespace),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                resources=client.V1ResourceRequirements(requests={"storage": f"{size_mb}Mi"}),
            ),
        )
        await self._core_v1.create_namespaced_persistent_volume_claim(namespace, pvc)

    async def _ensure_no_stale_pod(self, namespace: str) -> None:
        """A prior session for this workspace may have left its Pod behind (e.g. a
        crash before destroy() ran) — `_create_pod()` always creates fresh, so a
        leftover must be cleared first or creation fails with a 409 AlreadyExists."""
        try:
            await self._core_v1.read_namespaced_pod(_POD_NAME, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise
        await self._delete_pod_only(namespace)
        await self._wait_pod_gone(namespace)

    async def _wait_pod_gone(self, namespace: str, *, timeout_seconds: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                await self._core_v1.read_namespaced_pod(_POD_NAME, namespace)
            except ApiException as exc:
                if exc.status == 404:
                    return
                raise
            await asyncio.sleep(0.5)
        raise ProvisionerError(f"timed out waiting for stale pod in {namespace!r} to be removed")

    async def _delete_pod_only(self, namespace: str) -> None:
        with contextlib.suppress(ApiException):
            await self._core_v1.delete_namespaced_pod(_POD_NAME, namespace)

    async def _cleanup_after_acquire_failure(self, namespace: str, *, persistent: bool) -> None:
        """Ephemeral: the namespace was just created for this one attempt, so tearing
        the whole thing down is correct (existing behavior). Persistent: the namespace
        holds a durable PVC and may predate this attempt (a returning workspace) — only
        the pod this attempt tried to create is this attempt's to clean up."""
        if persistent:
            await self._delete_pod_only(namespace)
        else:
            await self._cleanup_namespace(namespace)

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
        # "pods": "1" still holds — a sidecar is another *container* in the one
        # sandbox Pod, not a second Pod. cpu/memory must cover main + every sidecar's
        # request, or admission rejects the pod the moment a sidecar is attached.
        total_cpu, total_memory = _total_resources(spec)
        hard = {
            "pods": "1",
            "requests.cpu": total_cpu,
            "requests.memory": total_memory,
            "limits.cpu": total_cpu,
            "limits.memory": total_memory,
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
        # `default`/`default_request`: fallback for any container that omits its own
        # resources — none does today (main and every sidecar always set theirs
        # explicitly), so this is a defensive default, not load-bearing.
        # `max`: must allow the LARGEST single container's request, not just main's —
        # a sidecar asking for more than main alone would otherwise fail admission
        # even though the namespace-total ResourceQuota (see _create_resource_quota)
        # has room for it.
        default_limits = {"cpu": spec.resources.cpu, "memory": spec.resources.memory}
        max_cpu, max_memory = _max_single_container_resources(spec)
        limit_range = client.V1LimitRange(
            metadata=client.V1ObjectMeta(name="sandbox-limits", namespace=namespace),
            spec=client.V1LimitRangeSpec(
                limits=[
                    client.V1LimitRangeItem(
                        type="Container",
                        default=default_limits,
                        default_request=default_limits,
                        max={"cpu": max_cpu, "memory": max_memory},
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
                # Heavy-weight-class node segregation (doc §4.3, Phase 7) — empty for
                # every non-heavy spec (SandboxService only ever populates these for
                # `weight_class == HEAVY`), so this is a no-op change for every
                # existing light/standard pod.
                node_selector=dict(spec.node_selector) or None,
                tolerations=[client.V1Toleration(**t) for t in spec.tolerations] or None,
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
                    ),
                    *(self._sidecar_container(s) for s in spec.sidecars),
                ],
                # Disk-backed (not `medium: Memory`) emptyDir on purpose: a Memory-backed
                # emptyDir is tmpfs under the hood, and Docker's own tmpfs mounts needed an
                # explicit `exec` mount option to let a compiled language's `go run`-style
                # execve out of /tmp work at all (see docs/TASK_CHECKLIST.md Phase 1/2's
                # "5th bug"). Disk-backed emptyDir has no such noexec default, sidestepping
                # that entire class of bug instead of reproducing it.
                #
                # workdir gets the durable PVC instead of emptyDir when persistent (Phase
                # 7, doc §10.2) — every other writable path (e.g. /tmp) stays emptyDir
                # regardless, since only the workspace itself needs to survive sessions.
                volumes=[
                    client.V1Volume(
                        name=_volume_name(p),
                        persistent_volume_claim=(
                            client.V1PersistentVolumeClaimVolumeSource(claim_name=_WORKSPACE_PVC_NAME)
                            if spec.workspace_id is not None and p == spec.workdir
                            else None
                        ),
                        empty_dir=(
                            None
                            if spec.workspace_id is not None and p == spec.workdir
                            else client.V1EmptyDirVolumeSource()
                        ),
                    )
                    for p in spec.writable_paths
                ]
                + [
                    client.V1Volume(
                        name=_sidecar_volume_name(s.name, p), empty_dir=client.V1EmptyDirVolumeSource()
                    )
                    for s in spec.sidecars
                    for p in s.writable_paths
                ],
            ),
        )
        await self._core_v1.create_namespaced_pod(namespace, pod)

    def _sidecar_container(self, sidecar: SidecarSpec) -> client.V1Container:
        """A sidecar shares the pod's network namespace natively (containers in one
        Pod always do) — that alone gives main<->sidecar localhost reachability
        without touching the namespace's default-deny NetworkPolicy, which governs
        pod-to-pod traffic over the CNI, not same-pod loopback traffic (doc §3.3's
        `reachableFrom: same-pod-only`).

        Runs as its OWN uid/gid (sidecar.uid), overriding the pod-wide
        securityContext's _SANDBOX_UID — a database image has its own expected user
        and can't be forced into the sandbox's uid. Not read-only-root, unlike main:
        a vetted database image is trusted infra, not sandboxed user code, and needs
        to write well beyond one declared data directory (sockets, logs, temp files)
        just to start up.
        """
        resource_requests = {"cpu": sidecar.resources.cpu, "memory": sidecar.resources.memory}
        readiness_probe = None
        if sidecar.health_check:
            # initial_delay + period*failure_threshold ~= _POD_READY_TIMEOUT_SECONDS,
            # so a slow-starting sidecar gets roughly as long as _wait_ready itself
            # allows before the whole acquire() call times out anyway.
            readiness_probe = client.V1Probe(
                _exec=client.V1ExecAction(command=list(sidecar.health_check)),
                initial_delay_seconds=1,
                period_seconds=2,
                failure_threshold=30,
            )
        return client.V1Container(
            name=sidecar.name,
            image=sidecar.image,
            env=[client.V1EnvVar(name=k, value=v) for k, v in sidecar.env.items()],
            resources=client.V1ResourceRequirements(
                requests=resource_requests, limits=dict(resource_requests)
            ),
            ports=[client.V1ContainerPort(name=p.name, container_port=p.container_port) for p in sidecar.ports],
            security_context=client.V1SecurityContext(
                allow_privilege_escalation=False,
                capabilities=client.V1Capabilities(drop=["ALL"]),
                read_only_root_filesystem=False,
                run_as_non_root=True,
                run_as_user=sidecar.uid,
                run_as_group=sidecar.uid,
            ),
            volume_mounts=[
                client.V1VolumeMount(name=_sidecar_volume_name(sidecar.name, p), mount_path=p)
                for p in sidecar.writable_paths
            ],
            readiness_probe=readiness_probe,
        )

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
        """Graceful eradication: for an ephemeral sandbox, deleting the whole namespace
        cascades to the pod (and its NetworkPolicy/ResourceQuota/LimitRange) — nothing
        sandbox-scoped can leak once this call succeeds. For a persistent sandbox
        (`handle.persistent`), only the Pod is deleted — the namespace holds that
        workspace's durable PVC and must survive for its next session (doc §10.2,
        Phase 7); the workspace itself is reaped separately, by retention (archive/
        purge), never by a sandbox's own destroy(). Safe to call more than once either
        way: "already gone" is success."""
        if handle.persistent:
            await self._delete_pod_only(handle.native_ref)
            return
        try:
            await self._core_v1.delete_namespace(handle.native_ref)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise ProvisionerError(f"failed to destroy sandbox {handle.sandbox_id}: {exc}") from exc

    # -- persistent workspace retention (doc §10.2, Phase 7) --------------------------

    async def _run_against_workspace_pvc(
        self,
        workspace_id: str,
        archiver_image: str,
        build_command: Callable[[str], list[str]],
        *,
        read_only: bool,
        stdin: bytes = b"",
    ) -> _RawExecResult:
        """Shared plumbing for `archive_workspace`/`measure_workspace_usage`/
        `restore_workspace`: creates a short-lived throwaway pod (named `_POD_NAME` —
        no already-running sandbox to exec into by the time retention reaps an idle
        workspace, its own sandbox having already been destroyed) mounting the
        workspace's PVC, execs `build_command(mount_path)` via the same exec plumbing
        `recycle()`/`get_file()` already use, and always removes the pod afterward.
        Never touches the namespace itself — that's `delete_workspace_volume`'s job."""
        namespace = f"{self._namespace_prefix}ws-{workspace_id}"
        mount_path = "/workspace-mount"
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(name=_POD_NAME, namespace=namespace),
            spec=client.V1PodSpec(
                restart_policy="Never",
                automount_service_account_token=False,
                termination_grace_period_seconds=_TERMINATION_GRACE_SECONDS,
                security_context=client.V1PodSecurityContext(
                    run_as_non_root=True,
                    run_as_user=_SANDBOX_UID,
                    run_as_group=_SANDBOX_UID,
                    fs_group=_SANDBOX_UID,
                    seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                ),
                containers=[
                    client.V1Container(
                        name=_MAIN_CONTAINER,
                        image=archiver_image,
                        command=["sleep", "infinity"],
                        security_context=client.V1SecurityContext(
                            allow_privilege_escalation=False,
                            capabilities=client.V1Capabilities(drop=["ALL"]),
                            read_only_root_filesystem=True,
                        ),
                        volume_mounts=[
                            client.V1VolumeMount(name="workspace-src", mount_path=mount_path, read_only=read_only)
                        ],
                    )
                ],
                volumes=[
                    client.V1Volume(
                        name="workspace-src",
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=_WORKSPACE_PVC_NAME, read_only=read_only
                        ),
                    )
                ],
            ),
        )
        try:
            await self._core_v1.create_namespaced_pod(namespace, pod)
            await self._wait_ready(namespace)
            return await self._run_exec_raw(
                namespace,
                build_command(mount_path),
                stdin=stdin,
                timeout_seconds=60,
                max_output_bytes=20 * 1024**3,  # generous — doc §10.2's default quota is 10 GiB
            )
        except ApiException as exc:
            raise ProvisionerError(f"failed to run against workspace {workspace_id} PVC: {exc}") from exc
        finally:
            await self._delete_pod_only(namespace)

    async def archive_workspace(self, workspace_id: str, *, archiver_image: str) -> bytes:
        raw = await self._run_against_workspace_pvc(
            workspace_id,
            archiver_image,
            lambda mount_path: ["tar", "czf", "-", "-C", mount_path, "."],
            read_only=True,
        )
        if raw.exit_code or raw.truncated:
            raise ProvisionerError(
                f"failed to archive workspace {workspace_id}: tar exited {raw.exit_code} (truncated={raw.truncated})"
            )
        return raw.stdout

    async def measure_workspace_usage(self, workspace_id: str, *, archiver_image: str) -> int:
        raw = await self._run_against_workspace_pvc(
            workspace_id, archiver_image, lambda mount_path: ["du", "-sm", mount_path], read_only=True
        )
        if raw.exit_code:
            raise ProvisionerError(f"failed to measure workspace {workspace_id} usage: du exited {raw.exit_code}")
        return int(raw.stdout.split()[0])

    async def restore_workspace(self, workspace_id: str, data: bytes, *, archiver_image: str) -> None:
        """Symmetric to `archive_workspace()`. Unlike Docker (where a bare
        `volumes.create()` suffices), `delete_workspace_volume()` already removed the
        *entire namespace* holding the PVC — so this recreates the namespace shell
        first (with placeholder resource sizing; a real `create_sandbox(persistent=
        True)` call never resizes an existing namespace's quota/limits either — sized
        once, at first creation, a known characteristic of this design, not something
        restore introduces) before mounting it writable and untarring `data` in."""
        namespace = f"{self._namespace_prefix}ws-{workspace_id}"
        try:
            await self._core_v1.read_namespace(namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise ProvisionerError(f"failed to restore workspace {workspace_id}: {exc}") from exc
            placeholder_spec = SandboxSpec(
                image=archiver_image,
                command=["sleep", "infinity"],
                resources=ResourceSpec(cpu="1", memory="512Mi"),
                workspace_id=workspace_id,
            )
            await self._ensure_persistent_namespace(namespace, placeholder_spec)

        raw = await self._run_against_workspace_pvc(
            workspace_id,
            archiver_image,
            lambda mount_path: ["tar", "xzf", "-", "-C", mount_path],
            read_only=False,
            stdin=data,
        )
        if raw.exit_code:
            raise ProvisionerError(
                f"failed to restore workspace {workspace_id}: tar exited {raw.exit_code}: {raw.stderr!r}"
            )

    async def delete_workspace_volume(self, workspace_id: str) -> None:
        """The workspace's namespace holds nothing but that one workspace's PVC (+
        NetworkPolicy/ResourceQuota/LimitRange) — deleting it is the correct, total
        teardown, not an over-broad one."""
        await self._cleanup_namespace(f"{self._namespace_prefix}ws-{workspace_id}")

    # -- orphan GC (doc §4.1, Phase 7) -------------------------------------------------

    async def list_sandbox_refs(self) -> list[NativeSandboxRef]:
        namespaces = await self._core_v1.list_namespace(label_selector="io.kubesandbox.sandbox-id")
        refs = []
        for ns in namespaces.items:
            sandbox_id = (ns.metadata.labels or {}).get("io.kubesandbox.sandbox-id")
            if not sandbox_id:
                continue
            # Sidecars are just other containers in the same Pod/namespace, not
            # separate namespaces — deleting the namespace (destroy()'s existing
            # ephemeral-path behavior) already reaps them, so there's no analogous
            # sidecar_refs to populate here, unlike Docker.
            refs.append(
                NativeSandboxRef(sandbox_id=sandbox_id, native_ref=ns.metadata.name, created_at=ns.metadata.creation_timestamp)
            )
        return refs

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
        if target != "main" and target not in handle.sidecar_refs:
            raise ProvisionerError(f"sandbox {handle.sandbox_id} has no sidecar named {target!r}")
        container = _MAIN_CONTAINER if target == "main" else target
        try:
            return await self._run_exec(
                handle.native_ref,
                command,
                stdin=stdin,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
                container=container,
            )
        except SandboxNotFoundError:
            raise
        except ApiException as exc:
            raise ProvisionerError(f"failed to start exec in {target!r}: {exc}") from exc

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
        container: str = _MAIN_CONTAINER,
    ) -> BatchRunResult:
        raw = await self._run_exec_raw(
            namespace,
            command,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            container=container,
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

    async def _run_exec_raw(
        self,
        namespace: str,
        command: list[str],
        *,
        stdin: bytes,
        timeout_seconds: int,
        max_output_bytes: int,
        container: str = _MAIN_CONTAINER,
    ) -> _RawExecResult:
        """Same exec/demux plumbing as `_run_exec`, but returns raw bytes instead of
        lossily-decoded str — get_file() needs this so downloading a binary file
        doesn't get corrupted by a `errors="replace"` UTF-8 decode. `container`
        defaults to `_MAIN_CONTAINER`; exec_in() (Phase 5) passes a sidecar's name
        instead to run admin commands against it."""
        try:
            ws_ctx = await self._exec_v1.connect_get_namespaced_pod_exec(
                _POD_NAME,
                namespace,
                container=container,
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

        return _RawExecResult(
            exit_code=exit_code,
            stdout=bytes(stdout_buf),
            stderr=bytes(stderr_buf),
            duration_ms=duration_ms,
            truncated=truncated,
            timed_out=timed_out,
        )

    async def get_file(self, handle: SandboxHandle, path: str) -> bytes:
        raw = await self._run_exec_raw(
            handle.native_ref,
            ["cat", path],
            stdin=b"",
            timeout_seconds=_FILE_OP_TIMEOUT_SECONDS,
            max_output_bytes=10_000_000,
        )
        if raw.exit_code:
            raise ProvisionerError(
                f"failed to read {path!r} (exit {raw.exit_code}): "
                f"{raw.stderr.decode(errors='replace')!r}"
            )
        return raw.stdout

    async def list_tree(self, handle: SandboxHandle, path: str) -> list[FileEntry]:
        result = await self._run_exec(
            handle.native_ref,
            ["find", path, "-mindepth", "1", "-printf", "%y|%P\n"],
            stdin=b"",
            timeout_seconds=_FILE_OP_TIMEOUT_SECONDS,
            max_output_bytes=5_000_000,
        )
        if result.exit_code:
            raise ProvisionerError(
                f"failed to list {path!r} (exit {result.exit_code}): {result.stderr or result.stdout!r}"
            )
        return parse_find_output(result.stdout)

    # -- interactive (Phase 4) -----------------------------------------------------------

    async def attach(self, handle: SandboxHandle) -> PTYStream:
        try:
            ws_ctx = await self._exec_v1.connect_get_namespaced_pod_exec(
                _POD_NAME,
                handle.native_ref,
                container=_MAIN_CONTAINER,
                command=_INTERACTIVE_SHELL_CMD,
                stderr=True,
                stdin=True,
                stdout=True,
                tty=True,
                _preload_content=False,
            )
        except ApiException as exc:
            if exc.status == 404:
                raise SandboxNotFoundError(handle.sandbox_id) from exc
            raise ProvisionerError(f"failed to open interactive attach: {exc}") from exc

        # Held open for the PTYStream's whole lifetime (unlike exec_batch's `async
        # with`, which is scoped to one bundled call) — closed explicitly by
        # KubernetesPTYStream.close().
        ws = await ws_ctx.__aenter__()
        return KubernetesPTYStream(ws_ctx, ws)


class KubernetesPTYStream:
    """Wraps the same channel-framed exec WebSocket `exec_batch`/`_run_exec` use, but
    held open for a whole interactive session instead of one bundled call. Under
    tty=True the remote PTY still merges stdout/stderr (real PTY semantics, matching
    Docker's tty behavior — see docker.py's DockerPTYStream) but the server keeps
    sending the exit-status control frame on ERROR_CHANNEL exactly as it does for
    non-tty exec, so exit detection reuses that same frame."""

    def __init__(self, ws_ctx, ws) -> None:
        self._ws_ctx = ws_ctx
        self._ws = ws
        self._exited = False

    async def write_stdin(self, data: bytes) -> None:
        await self._ws.send_bytes(bytes([STDIN_CHANNEL]) + data)

    async def resize(self, *, cols: int, rows: int) -> None:
        payload = json.dumps({"Width": cols, "Height": rows}).encode()
        await self._ws.send_bytes(bytes([RESIZE_CHANNEL]) + payload)

    async def read(self) -> PTYEvent | None:
        if self._exited:
            return None
        async for msg in self._ws:
            if msg.type not in (WSMsgType.BINARY, WSMsgType.TEXT):
                continue
            data = msg.data if isinstance(msg.data, (bytes, bytearray)) else msg.data.encode()
            if not data:
                continue
            channel, payload = data[0], data[1:]
            if channel in (STDOUT_CHANNEL, STDERR_CHANNEL):
                return PTYEvent(kind="output", data=bytes(payload))
            if channel == ERROR_CHANNEL:
                self._exited = True
                return PTYEvent(kind="exit", exit_code=WsApiClient.parse_error_data(bytes(payload)))
        self._exited = True
        return None

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws_ctx.__aexit__(None, None, None)
