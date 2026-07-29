"""Unit tests for DockerProvisioner.acquire()'s persistent-workspace wiring (Phase 7)
— no live daemon involved. `self._docker` is swapped for a fake exposing just the
surface acquire() touches (`volumes.create`, `containers.run`), mirroring how
test_kubernetes_provisioner.py mocks `_core_v1`/`_exec_v1` instead of a real cluster.
Live-daemon behavior (does a fresh named volume's ownership actually end up
10001:10001, does the mount really survive a container remove) is a separate,
relayed-verification concern — see docs/TASK_CHECKLIST.md's Phase 7 section.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import ProvisionerError
from app.domain.execution import ResourceSpec, SandboxSpec
from app.provisioners.docker import DockerProvisioner


def make_spec(**overrides) -> SandboxSpec:
    defaults = dict(
        image="kubesandbox/python:3.12.4-slim",
        command=["sleep", "infinity"],
        resources=ResourceSpec(cpu="500m", memory="256Mi"),
        writable_paths=["/workspace", "/tmp"],
        workdir="/workspace",
    )
    defaults.update(overrides)
    return SandboxSpec(**defaults)


class FakeExecStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self._resp = None  # _half_close_stdin no-ops gracefully when this is None
        self.written: list[bytes] = []

    async def read_out(self):
        if not self._chunks:
            return None
        return SimpleNamespace(data=self._chunks.pop(0))

    async def _init(self) -> None:
        pass

    async def write_in(self, data: bytes) -> None:
        self.written.append(data)

    async def close(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeExec:
    def __init__(self, *, exit_code: int = 0, output: bytes = b"") -> None:
        self.exit_code = exit_code
        self.output = output
        self.calls: list[dict] = []

    def start(self, detach: bool = False):
        return FakeExecStream([self.output] if self.output else [])

    async def inspect(self):
        return {"ExitCode": self.exit_code}


class FakeContainer:
    def __init__(self, container_id: str = "container-1") -> None:
        self.id = container_id
        self.exec_calls: list[dict] = []
        self._exec_result = FakeExec()

    async def show(self):
        return {"State": {"Running": True}}

    async def exec(self, cmd, *, stdin=False, stdout=True, stderr=True, user=None):
        self.exec_calls.append({"cmd": cmd, "user": user})
        return self._exec_result

    async def delete(self, **kwargs):
        pass

    async def stop(self, **kwargs):
        pass


@pytest.fixture
def provisioner():
    docker = MagicMock()
    docker.volumes.create = AsyncMock()
    docker.containers = MagicMock()
    docker.containers.container = MagicMock(return_value=FakeContainer())
    return DockerProvisioner(docker=docker)


async def test_acquire_without_workspace_id_uses_tmpfs_for_everything(provisioner, monkeypatch):
    container = FakeContainer()
    provisioner._docker.containers.run = AsyncMock(return_value=container)

    handle = await provisioner.acquire(make_spec())

    provisioner._docker.volumes.create.assert_not_called()
    run_config = provisioner._docker.containers.run.call_args.args[0]
    host_config = run_config["HostConfig"]
    assert set(host_config["Tmpfs"].keys()) == {"/workspace", "/tmp"}
    assert "Mounts" not in host_config
    assert "CapAdd" not in host_config
    assert handle.persistent is False
    assert container.exec_calls == []  # no ownership-fix exec needed


async def test_acquire_with_workspace_id_mounts_named_volume_and_fixes_ownership(provisioner):
    container = FakeContainer()
    provisioner._docker.containers.run = AsyncMock(return_value=container)

    handle = await provisioner.acquire(make_spec(workspace_id="ws-123"))

    provisioner._docker.volumes.create.assert_awaited_once_with({"Name": "kubesandbox-ws-ws-123"})

    run_config = provisioner._docker.containers.run.call_args.args[0]
    host_config = run_config["HostConfig"]
    # /workspace is now a named volume mount, not tmpfs — /tmp stays tmpfs.
    assert set(host_config["Tmpfs"].keys()) == {"/tmp"}
    assert host_config["Mounts"] == [
        {"Type": "volume", "Source": "kubesandbox-ws-ws-123", "Target": "/workspace", "ReadOnly": False}
    ]
    assert host_config["CapAdd"] == ["CHOWN"]

    assert handle.persistent is True
    assert len(container.exec_calls) == 1
    assert container.exec_calls[0]["cmd"] == ["chown", "10001:10001", "/workspace"]
    assert container.exec_calls[0]["user"] == "0:0"


async def test_archive_workspace_tars_via_exec_and_removes_container(provisioner):
    container = FakeContainer()
    container._exec_result = FakeExec(exit_code=0, output=b"tar-bytes-here")
    provisioner._docker.containers.run = AsyncMock(return_value=container)
    removed: list[str] = []

    async def fake_force_remove(container_id):
        removed.append(container_id)

    provisioner._force_remove = fake_force_remove

    data = await provisioner.archive_workspace("ws-123", archiver_image="kubesandbox/base:1.0")

    assert data == b"tar-bytes-here"
    run_config = provisioner._docker.containers.run.call_args.args[0]
    assert run_config["Image"] == "kubesandbox/base:1.0"
    assert run_config["HostConfig"]["Mounts"] == [
        {"Type": "volume", "Source": "kubesandbox-ws-ws-123", "Target": "/workspace-src", "ReadOnly": True}
    ]
    assert container.exec_calls[0]["cmd"] == ["tar", "czf", "-", "-C", "/workspace-src", "."]
    assert removed == [container.id]


async def test_archive_workspace_raises_and_still_removes_container_on_tar_failure(provisioner):
    container = FakeContainer()
    container._exec_result = FakeExec(exit_code=2, output=b"tar: error")
    provisioner._docker.containers.run = AsyncMock(return_value=container)
    removed: list[str] = []

    async def fake_force_remove(container_id):
        removed.append(container_id)

    provisioner._force_remove = fake_force_remove

    with pytest.raises(ProvisionerError, match="failed to archive workspace"):
        await provisioner.archive_workspace("ws-123", archiver_image="kubesandbox/base:1.0")

    assert removed == [container.id]


async def test_measure_workspace_usage_parses_du_output(provisioner):
    container = FakeContainer()
    container._exec_result = FakeExec(exit_code=0, output=b"42\t/workspace-src\n")
    provisioner._docker.containers.run = AsyncMock(return_value=container)
    provisioner._force_remove = AsyncMock()

    usage_mb = await provisioner.measure_workspace_usage("ws-123", archiver_image="kubesandbox/base:1.0")

    assert usage_mb == 42
    assert container.exec_calls[0]["cmd"] == ["du", "-sm", "/workspace-src"]


async def test_restore_workspace_recreates_volume_and_untars_stdin(provisioner):
    container = FakeContainer()
    container._exec_result = FakeExec(exit_code=0)
    provisioner._docker.containers.run = AsyncMock(return_value=container)
    provisioner._force_remove = AsyncMock()

    await provisioner.restore_workspace("ws-123", b"tar-payload", archiver_image="kubesandbox/base:1.0")

    provisioner._docker.volumes.create.assert_awaited_once_with({"Name": "kubesandbox-ws-ws-123"})
    run_config = provisioner._docker.containers.run.call_args.args[0]
    assert run_config["HostConfig"]["Mounts"] == [
        {"Type": "volume", "Source": "kubesandbox-ws-ws-123", "Target": "/workspace-restore", "ReadOnly": False}
    ]
    assert run_config["HostConfig"]["CapAdd"] == ["CHOWN"]
    assert container.exec_calls[0]["cmd"] == ["tar", "xzf", "-", "-C", "/workspace-restore"]
    # Ownership-fix chown ran after the untar (2nd exec call, same as a fresh acquire()).
    assert container.exec_calls[1]["cmd"] == ["chown", "10001:10001", "/workspace-restore"]


async def test_restore_workspace_raises_on_untar_failure(provisioner):
    container = FakeContainer()
    container._exec_result = FakeExec(exit_code=2, output=b"tar: corrupt")
    provisioner._docker.containers.run = AsyncMock(return_value=container)
    provisioner._force_remove = AsyncMock()

    with pytest.raises(ProvisionerError, match="failed to restore workspace"):
        await provisioner.restore_workspace("ws-123", b"bad-payload", archiver_image="kubesandbox/base:1.0")


async def test_delete_workspace_volume_removes_named_volume(provisioner):
    deleted = {}

    class FakeVolume:
        def __init__(self, docker, name):
            deleted["name"] = name

        async def delete(self, force=False):
            deleted["called"] = True

    import app.provisioners.docker as docker_module

    original = docker_module.DockerVolume
    docker_module.DockerVolume = FakeVolume
    try:
        await provisioner.delete_workspace_volume("ws-123")
    finally:
        docker_module.DockerVolume = original

    assert deleted == {"name": "kubesandbox-ws-ws-123", "called": True}


async def test_acquire_raises_and_cleans_up_when_ownership_fix_fails(provisioner):
    container = FakeContainer()
    container._exec_result = FakeExec(exit_code=1, output=b"chown: not permitted")
    provisioner._docker.containers.run = AsyncMock(return_value=container)
    removed: list[str] = []

    async def fake_force_remove(container_id):
        removed.append(container_id)

    provisioner._force_remove = fake_force_remove

    with pytest.raises(ProvisionerError, match="failed to fix ownership"):
        await provisioner.acquire(make_spec(workspace_id="ws-123"))

    assert removed == [container.id]
