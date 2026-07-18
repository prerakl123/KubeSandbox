"""Unit tests for KubernetesProvisioner — no live cluster involved. The K8s API client
attributes (`_core_v1`, `_networking_v1`, `_exec_v1`) are swapped for AsyncMocks/fakes so
these tests exercise the pod/namespace spec construction and the exec frame-demuxing
logic in isolation. Live-cluster behavior (the exec protocol's v5 close-channel quirk in
particular) is covered separately by tests/integration/test_execute_kubernetes.py against
a real kind cluster — that distinction matters here, mirroring how the Docker
integration test exists precisely because a fake can't catch real-daemon bugs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import WSMsgType
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from app.core.errors import ProvisionerError, SandboxNotFoundError
from app.domain.execution import (
    BatchCommand,
    ResourceSpec,
    SandboxHandle,
    SandboxSpec,
    SandboxState,
    SidecarSpec,
)
from app.provisioners.kubernetes import KubernetesProvisioner


def make_spec(**overrides) -> SandboxSpec:
    defaults = dict(
        image="kubesandbox/python:3.12.4-slim",
        command=["sleep", "infinity"],
        env={"FOO": "bar"},
        workdir="/workspace",
        writable_paths=["/workspace", "/tmp"],
        read_only_root_filesystem=True,
        resources=ResourceSpec(cpu="500m", memory="256Mi"),
        labels={"io.kubesandbox.component": "python@3.12.4"},
    )
    defaults.update(overrides)
    return SandboxSpec(**defaults)


def make_sidecar(**overrides) -> SidecarSpec:
    defaults = dict(
        name="postgresql",
        image="postgres:16-alpine",
        env={"POSTGRES_PASSWORD": "bootstrap"},
        resources=ResourceSpec(cpu="100m", memory="128Mi"),
        writable_paths=["/var/lib/postgresql/data"],
        health_check=["pg_isready", "-U", "postgres"],
        uid=999,
    )
    defaults.update(overrides)
    return SidecarSpec(**defaults)


@pytest.fixture
async def provisioner():
    p = KubernetesProvisioner(client.Configuration(), namespace_prefix="kubesandbox-sb-", runtime_class=None)
    p._core_v1 = AsyncMock()
    p._networking_v1 = AsyncMock()
    p._exec_v1 = AsyncMock()
    yield p
    await p.aclose()


def _running_pod():
    return SimpleNamespace(
        status=SimpleNamespace(phase="Running", container_statuses=[SimpleNamespace(ready=True)])
    )


class FakeWsMessage:
    def __init__(self, msg_type, data):
        self.type = msg_type
        self.data = data


class FakeWsConn:
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent: list[bytes] = []
        self.hang = False

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.hang:
            import asyncio

            await asyncio.sleep(999)
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class FakeWsCtx:
    def __init__(self, conn: FakeWsConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc_info):
        return False


# -- acquire() / pod spec rendering --------------------------------------------------


async def test_acquire_creates_hardened_namespace_scoped_resources(provisioner):
    provisioner._core_v1.read_namespaced_pod.return_value = _running_pod()

    handle = await provisioner.acquire(make_spec())

    assert handle.backend == "kubernetes"
    assert handle.native_ref.startswith("kubesandbox-sb-")

    ns_call = provisioner._core_v1.create_namespace.call_args
    assert ns_call.args[0].metadata.name == handle.native_ref

    netpol = provisioner._networking_v1.create_namespaced_network_policy.call_args
    assert netpol.args[0] == handle.native_ref
    netpol_body = netpol.args[1]
    assert netpol_body.spec.policy_types == ["Ingress", "Egress"]
    assert netpol_body.spec.ingress == []
    assert netpol_body.spec.egress == []

    quota_call = provisioner._core_v1.create_namespaced_resource_quota.call_args
    assert quota_call.args[1].spec.hard["requests.cpu"] == "500m"
    assert quota_call.args[1].spec.hard["pods"] == "1"

    pod_call = provisioner._core_v1.create_namespaced_pod.call_args
    pod_spec = pod_call.args[1].spec
    container = pod_spec.containers[0]

    assert pod_spec.security_context.run_as_non_root is True
    assert pod_spec.security_context.run_as_user == 10001
    assert pod_spec.security_context.run_as_group == 10001
    assert pod_spec.security_context.fs_group == 10001
    assert pod_spec.security_context.seccomp_profile.type == "RuntimeDefault"
    assert pod_spec.automount_service_account_token is False
    assert pod_spec.runtime_class_name is None

    assert container.security_context.allow_privilege_escalation is False
    assert container.security_context.capabilities.drop == ["ALL"]
    assert container.security_context.read_only_root_filesystem is True
    assert {vm.mount_path for vm in container.volume_mounts} == {"/workspace", "/tmp"}
    assert container.resources.requests == {"cpu": "500m", "memory": "256Mi"}


async def test_acquire_composes_sidecar_container_with_own_uid_and_readiness_probe(provisioner):
    provisioner._core_v1.read_namespaced_pod.return_value = _running_pod()

    handle = await provisioner.acquire(make_spec(sidecars=[make_sidecar()]))

    assert handle.sidecar_refs == {"postgresql": "postgresql"}

    pod_call = provisioner._core_v1.create_namespaced_pod.call_args
    pod_spec = pod_call.args[1].spec
    assert [c.name for c in pod_spec.containers] == ["main", "postgresql"]

    sidecar = pod_spec.containers[1]
    assert sidecar.image == "postgres:16-alpine"
    # Not forced into the pod-wide sandbox uid (10001) — a DB image runs as its own uid.
    assert sidecar.security_context.run_as_user == 999
    assert sidecar.security_context.run_as_group == 999
    assert sidecar.security_context.read_only_root_filesystem is False
    assert sidecar.readiness_probe._exec.command == ["pg_isready", "-U", "postgres"]
    assert {vm.mount_path for vm in sidecar.volume_mounts} == {"/var/lib/postgresql/data"}

    volume_names = {v.name for v in pod_spec.volumes}
    sidecar_volume_names = {vm.name for vm in sidecar.volume_mounts}
    # Sidecar's volume is present under its own namespaced name and doesn't collide
    # with main's volume names.
    assert sidecar_volume_names < volume_names
    main_volume_names = {vm.name for vm in pod_spec.containers[0].volume_mounts}
    assert not (sidecar_volume_names & main_volume_names)


async def test_acquire_resource_quota_and_limit_range_account_for_sidecars(provisioner):
    provisioner._core_v1.read_namespaced_pod.return_value = _running_pod()

    await provisioner.acquire(make_spec(sidecars=[make_sidecar()]))

    quota = provisioner._core_v1.create_namespaced_resource_quota.call_args.args[1].spec.hard
    assert quota["requests.cpu"] == "600m"  # 500m main + 100m sidecar
    assert quota["requests.memory"] == "384Mi"  # 256Mi main + 128Mi sidecar
    assert quota["pods"] == "1"  # still one Pod, just more containers in it

    limit_range = provisioner._core_v1.create_namespaced_limit_range.call_args.args[1].spec.limits[0]
    # max must allow the LARGEST single container (main's 500m/256Mi here, since the
    # sidecar asks for less) even though the namespace total above is higher.
    assert limit_range.max == {"cpu": "500m", "memory": "256Mi"}


async def test_acquire_wires_runtime_class_when_configured(provisioner):
    provisioner._runtime_class = "gvisor"
    provisioner._core_v1.read_namespaced_pod.return_value = _running_pod()

    await provisioner.acquire(make_spec())

    pod_call = provisioner._core_v1.create_namespaced_pod.call_args
    assert pod_call.args[1].spec.runtime_class_name == "gvisor"


async def test_acquire_cleans_up_namespace_on_pod_creation_failure(provisioner):
    provisioner._core_v1.create_namespaced_pod.side_effect = ApiException(status=500, reason="boom")

    with pytest.raises(ProvisionerError):
        await provisioner.acquire(make_spec())

    assert provisioner._core_v1.delete_namespace.await_count == 1


async def test_acquire_raises_when_pod_never_becomes_ready(provisioner, monkeypatch):
    monkeypatch.setattr("app.provisioners.kubernetes._POD_READY_TIMEOUT_SECONDS", 0)
    provisioner._core_v1.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(phase="Pending", container_statuses=None)
    )

    with pytest.raises(ProvisionerError, match="timed out"):
        await provisioner.acquire(make_spec())

    assert provisioner._core_v1.delete_namespace.await_count == 1


# -- status() / destroy() ------------------------------------------------------------


async def test_status_maps_running_to_active(provisioner):
    provisioner._core_v1.read_namespaced_pod.return_value = _running_pod()
    status = await provisioner.status(_handle())
    assert status.state == SandboxState.ACTIVE


async def test_status_maps_404_to_terminated(provisioner):
    provisioner._core_v1.read_namespaced_pod.side_effect = ApiException(status=404)
    status = await provisioner.status(_handle())
    assert status.state == SandboxState.TERMINATED


async def test_destroy_is_idempotent_on_already_gone(provisioner):
    provisioner._core_v1.delete_namespace.side_effect = ApiException(status=404)
    await provisioner.destroy(_handle())  # must not raise


async def test_destroy_raises_provisioner_error_on_real_failure(provisioner):
    provisioner._core_v1.delete_namespace.side_effect = ApiException(status=500, reason="etcd sad")
    with pytest.raises(ProvisionerError):
        await provisioner.destroy(_handle())


# -- exec frame demuxing --------------------------------------------------------------


def _handle(namespace="ns1"):
    return SandboxHandle(sandbox_id="s1", backend="kubernetes", native_ref=namespace, created_at=datetime.now(UTC))


async def test_exec_batch_splits_stdout_stderr_and_parses_exit_code(provisioner):
    conn = FakeWsConn(
        [
            FakeWsMessage(WSMsgType.BINARY, bytes([1]) + b"hello-out"),
            FakeWsMessage(WSMsgType.BINARY, bytes([2]) + b"hello-err"),
            FakeWsMessage(WSMsgType.BINARY, bytes([3]) + b'{"metadata":{},"status":"Success"}'),
        ]
    )
    provisioner._exec_v1.connect_get_namespaced_pod_exec.return_value = FakeWsCtx(conn)

    result = await provisioner.exec_batch(
        _handle(), BatchCommand(command=["echo", "hi"], stdin="", timeout_seconds=5, max_output_bytes=1000)
    )

    assert result.stdout == "hello-out"
    assert result.stderr == "hello-err"
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.truncated is False


async def test_exec_batch_parses_nonzero_exit_code(provisioner):
    conn = FakeWsConn(
        [
            FakeWsMessage(
                WSMsgType.BINARY,
                bytes([3])
                + b'{"metadata":{},"status":"Failure","details":{"causes":[{"message":"7"}]}}',
            ),
        ]
    )
    provisioner._exec_v1.connect_get_namespaced_pod_exec.return_value = FakeWsCtx(conn)

    result = await provisioner.exec_batch(
        _handle(), BatchCommand(command=["sh", "-c", "exit 7"], timeout_seconds=5, max_output_bytes=1000)
    )
    assert result.exit_code == 7


async def test_exec_batch_truncates_but_keeps_draining_for_exit_code(provisioner):
    conn = FakeWsConn(
        [
            FakeWsMessage(WSMsgType.BINARY, bytes([1]) + b"0123456789"),
            FakeWsMessage(WSMsgType.BINARY, bytes([1]) + b"more-that-overflows"),
            FakeWsMessage(WSMsgType.BINARY, bytes([3]) + b'{"metadata":{},"status":"Success"}'),
        ]
    )
    provisioner._exec_v1.connect_get_namespaced_pod_exec.return_value = FakeWsCtx(conn)

    result = await provisioner.exec_batch(
        _handle(), BatchCommand(command=["echo"], timeout_seconds=5, max_output_bytes=10)
    )
    assert result.truncated is True
    assert result.exit_code == 0
    assert len(result.stdout) == 10


async def test_exec_batch_times_out_when_no_exit_status_arrives(provisioner):
    conn = FakeWsConn([])
    conn.hang = True
    provisioner._exec_v1.connect_get_namespaced_pod_exec.return_value = FakeWsCtx(conn)

    result = await provisioner.exec_batch(
        _handle(), BatchCommand(command=["sleep", "999"], timeout_seconds=1, max_output_bytes=1000)
    )
    assert result.timed_out is True
    assert result.exit_code == 124


async def test_exec_batch_sends_stdin_then_close_channel_signal(provisioner):
    conn = FakeWsConn(
        [FakeWsMessage(WSMsgType.BINARY, bytes([3]) + b'{"metadata":{},"status":"Success"}')]
    )
    provisioner._exec_v1.connect_get_namespaced_pod_exec.return_value = FakeWsCtx(conn)

    await provisioner.exec_batch(
        _handle(), BatchCommand(command=["cat"], stdin="hello", timeout_seconds=5, max_output_bytes=1000)
    )

    assert conn.sent[0] == bytes([0]) + b"hello"  # STDIN_CHANNEL = 0
    assert conn.sent[1] == bytes([255, 0])  # close-channel-index signal for stdin


async def test_exec_batch_raises_sandbox_not_found_on_404(provisioner):
    provisioner._exec_v1.connect_get_namespaced_pod_exec.side_effect = ApiException(status=404)

    with pytest.raises(SandboxNotFoundError):
        await provisioner.exec_batch(_handle(), BatchCommand(command=["echo", "hi"]))


# -- exec_in (Phase 5, doc §3.5's ComponentHook / healthcheck admin-exec) -------------------


async def test_exec_in_targets_sidecar_container_by_name(provisioner):
    conn = FakeWsConn(
        [FakeWsMessage(WSMsgType.BINARY, bytes([3]) + b'{"metadata":{},"status":"Success"}')]
    )
    provisioner._exec_v1.connect_get_namespaced_pod_exec.return_value = FakeWsCtx(conn)

    handle = SandboxHandle(
        sandbox_id="s1", backend="kubernetes", native_ref="ns1",
        created_at=datetime.now(UTC), sidecar_refs={"postgresql": "postgresql"},
    )
    await provisioner.exec_in(handle, "postgresql", ["pg_isready", "-U", "postgres"])

    call = provisioner._exec_v1.connect_get_namespaced_pod_exec.call_args
    assert call.kwargs["container"] == "postgresql"


async def test_exec_in_targets_main_container(provisioner):
    conn = FakeWsConn(
        [FakeWsMessage(WSMsgType.BINARY, bytes([3]) + b'{"metadata":{},"status":"Success"}')]
    )
    provisioner._exec_v1.connect_get_namespaced_pod_exec.return_value = FakeWsCtx(conn)

    await provisioner.exec_in(_handle(), "main", ["echo", "hi"])

    call = provisioner._exec_v1.connect_get_namespaced_pod_exec.call_args
    assert call.kwargs["container"] == "main"


async def test_exec_in_raises_on_unknown_sidecar_target(provisioner):
    handle = SandboxHandle(
        sandbox_id="s1", backend="kubernetes", native_ref="ns1",
        created_at=datetime.now(UTC), sidecar_refs={"postgresql": "postgresql"},
    )
    with pytest.raises(ProvisionerError, match="no sidecar named"):
        await provisioner.exec_in(handle, "redis", ["redis-cli", "ping"])

    provisioner._exec_v1.connect_get_namespaced_pod_exec.assert_not_called()


# -- file APIs (Phase 4, doc §5.4) -----------------------------------------------------


async def test_get_file_returns_raw_bytes(provisioner):
    conn = FakeWsConn(
        [
            FakeWsMessage(WSMsgType.BINARY, bytes([1]) + b"raw \xff bytes"),
            FakeWsMessage(WSMsgType.BINARY, bytes([3]) + b'{"metadata":{},"status":"Success"}'),
        ]
    )
    provisioner._exec_v1.connect_get_namespaced_pod_exec.return_value = FakeWsCtx(conn)

    content = await provisioner.get_file(_handle(), "/workspace/main.py")
    assert content == b"raw \xff bytes"  # not lossily decoded


async def test_get_file_raises_on_nonzero_exit(provisioner):
    conn = FakeWsConn(
        [
            FakeWsMessage(WSMsgType.BINARY, bytes([2]) + b"No such file"),
            FakeWsMessage(
                WSMsgType.BINARY,
                bytes([3]) + b'{"metadata":{},"status":"Failure","details":{"causes":[{"message":"1"}]}}',
            ),
        ]
    )
    provisioner._exec_v1.connect_get_namespaced_pod_exec.return_value = FakeWsCtx(conn)

    with pytest.raises(ProvisionerError, match="No such file"):
        await provisioner.get_file(_handle(), "/workspace/missing.py")


async def test_list_tree_parses_find_output(provisioner):
    conn = FakeWsConn(
        [
            FakeWsMessage(WSMsgType.BINARY, bytes([1]) + b"f|main.py\nd|sub\n"),
            FakeWsMessage(WSMsgType.BINARY, bytes([3]) + b'{"metadata":{},"status":"Success"}'),
        ]
    )
    provisioner._exec_v1.connect_get_namespaced_pod_exec.return_value = FakeWsCtx(conn)

    entries = await provisioner.list_tree(_handle(), "/workspace")
    assert {(e.path, e.is_dir) for e in entries} == {("main.py", False), ("sub", True)}


# -- interactive attach (Phase 4) -------------------------------------------------------


async def test_attach_opens_tty_exec_with_dtach_reattach_command(provisioner):
    conn = FakeWsConn([])
    provisioner._exec_v1.connect_get_namespaced_pod_exec.return_value = FakeWsCtx(conn)

    pty = await provisioner.attach(_handle())

    call = provisioner._exec_v1.connect_get_namespaced_pod_exec.call_args
    assert call.kwargs["tty"] is True
    assert call.kwargs["command"] == [
        "dtach", "-A", "/tmp/.kubesandbox-attach.sock", "-e", "none", "-z", "/bin/bash"
    ]
    await pty.close()


async def test_attach_pty_stream_relays_output_resize_and_exit(provisioner):
    conn = FakeWsConn(
        [
            FakeWsMessage(WSMsgType.BINARY, bytes([1]) + b"hello\n"),
            FakeWsMessage(WSMsgType.BINARY, bytes([3]) + b'{"metadata":{},"status":"Success"}'),
        ]
    )
    provisioner._exec_v1.connect_get_namespaced_pod_exec.return_value = FakeWsCtx(conn)

    pty = await provisioner.attach(_handle())

    await pty.write_stdin(b"ls\n")
    assert conn.sent[-1] == bytes([0]) + b"ls\n"  # STDIN_CHANNEL

    await pty.resize(cols=100, rows=40)
    assert conn.sent[-1] == bytes([4]) + b'{"Width": 100, "Height": 40}'  # RESIZE_CHANNEL

    event = await pty.read()
    assert event.kind == "output"
    assert event.data == b"hello\n"

    event = await pty.read()
    assert event.kind == "exit"
    assert event.exit_code == 0

    assert await pty.read() is None
    await pty.close()
