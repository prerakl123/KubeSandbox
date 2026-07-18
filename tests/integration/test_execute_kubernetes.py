"""Exercises the actual risky path — KubernetesProvisioner talking to a real cluster —
that unit tests can't cover with mocks (tests/unit/test_kubernetes_provisioner.py uses
AsyncMocks for the API client, which by construction can't catch a real cluster/exec
protocol quirk). Skips itself with a clear reason if `kubectl`/a reachable kind
cluster/the golden image aren't available (see README.md's kind setup steps,
deploy/kind/kind-config.yaml).

Deliberately bypasses the HTTP/TestClient layer, same reasoning as
test_execute_docker.py: calling SandboxService directly keeps this test focused on what
actually needs a live cluster to verify.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from app.extensions.loader import load_registry
from app.provisioners.kubernetes import KubernetesProvisioner
from app.services.sandbox_service import SandboxService

IMAGE_REF = "kubesandbox/python:3.12.4-slim"
KIND_CLUSTER = "kubesandbox-dev"
_NAMESPACE_PREFIX = "kubesandbox-sb-test-"


def _kubectl_cluster_reachable() -> bool:
    if shutil.which("kubectl") is None:
        return False
    try:
        result = subprocess.run(
            ["kubectl", "cluster-info", "--context", f"kind-{KIND_CLUSTER}"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _golden_image_loaded_on_kind_node() -> bool:
    """The image must be in the kind *node's* containerd store, not just the host
    Docker daemon — kind nodes don't share the host's image cache automatically."""
    if not _kubectl_cluster_reachable() or shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "exec", f"{KIND_CLUSTER}-control-plane", "crictl", "inspecti", IMAGE_REF],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    not _golden_image_loaded_on_kind_node(),
    reason=(
        "requires a reachable kind cluster (deploy/kind/kind-config.yaml) with the "
        f"python golden image loaded: kind load docker-image {IMAGE_REF} --name {KIND_CLUSTER}"
    ),
)


@pytest.fixture
async def provisioner():
    p = await KubernetesProvisioner.create(
        kubeconfig_path=None,  # falls back to default kubeconfig discovery
        namespace_prefix=_NAMESPACE_PREFIX,
        runtime_class=None,  # kind has no gVisor node — see deploy/manifests/base/runtimeclass.yaml
    )
    yield p
    await p.aclose()


async def test_execute_python_end_to_end(db_session, provisioner):
    registry = load_registry()
    service = SandboxService(registry, provisioner)

    result = await service.execute(
        language="python",
        code=(
            "x = 40\n"
            "result = x + 2\n"
            "print('hello from the pod')\n"
            "name = input()\n"
            "print('got:', name)\n"
        ),
        stdin="world",
        tenant_id="tenant-1",
        user_id=None,
        session=db_session,
    )

    assert result.exit_code == 0
    assert "hello from the pod" in result.stdout
    assert "got: world" in result.stdout
    assert result.stderr == ""
    assert result.timed_out is False
    assert result.variables == {"x": 40, "result": 42, "name": "world"}


async def test_execute_python_captures_exception_and_partial_variables(db_session, provisioner):
    registry = load_registry()
    service = SandboxService(registry, provisioner)

    result = await service.execute(
        language="python",
        code="partial = 'before crash'\nraise ValueError('boom')\n",
        tenant_id="tenant-1",
        user_id=None,
        session=db_session,
    )

    assert result.exit_code == 1
    assert "ValueError: boom" in result.stderr
    assert result.variables == {"partial": "before crash"}


async def test_execute_python_no_stdin_gives_immediate_eof(db_session, provisioner):
    """The core correctness property from doc §5.1, over the K8s exec protocol this
    time: a batch run never waits on a live client for stdin — see the v5 close-channel
    workaround documented in app/provisioners/kubernetes.py."""
    registry = load_registry()
    service = SandboxService(registry, provisioner)

    result = await service.execute(
        language="python",
        code="try:\n    input()\nexcept EOFError:\n    print('got EOF as expected')\n",
        tenant_id="tenant-1",
        user_id=None,
        session=db_session,
    )

    assert result.exit_code == 0
    assert result.timed_out is False
    assert "got EOF as expected" in result.stdout


async def test_no_leaked_sandbox_namespaces_after_runs():
    """Graceful eradication, live-verified: each run's namespace (holding its Pod,
    NetworkPolicy, ResourceQuota, LimitRange) must be gone after destroy(). Namespace
    deletion is asynchronous in Kubernetes (Terminating -> gone once GC finishes) and,
    confirmed live, how long that takes varies with API server/etcd load (a few seconds
    normally, longer right after a whole test session's worth of create/delete churn) —
    so this polls generously rather than asserting instantly or on a short timeout,
    which flakes on GC lag alone rather than catching an actual leak.
    """

    def _leaked() -> list[str]:
        result = subprocess.run(
            ["kubectl", "get", "namespaces", "-o", "name", "--context", f"kind-{KIND_CLUSTER}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return [
            line for line in result.stdout.splitlines() if line.startswith(f"namespace/{_NAMESPACE_PREFIX}")
        ]

    leaked = _leaked()
    for _ in range(60):
        if not leaked:
            break
        await asyncio.sleep(2)
        leaked = _leaked()

    assert leaked == [], f"leaked sandbox namespaces after test run: {leaked}"
