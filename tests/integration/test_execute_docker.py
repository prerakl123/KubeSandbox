"""Exercises the actual risky path — DockerProvisioner talking to a real container —
that unit tests can't cover with a fake. Skips itself with a clear reason if Docker
isn't reachable or the golden image hasn't been built yet (see README.md setup steps).

Deliberately bypasses the HTTP/TestClient layer: TestClient runs the ASGI app in a
separate thread with its own event loop, which doesn't mix safely with an asyncio DB
engine created in the test's own loop. Calling SandboxService directly keeps this test
focused on what actually needs a live daemon to verify.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.extensions.loader import load_registry
from app.provisioners.docker import DockerProvisioner
from app.services.sandbox_service import SandboxService

IMAGE_REF = "kubesandbox/python:3.12.4-slim"


def _docker_daemon_reachable() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _golden_image_built() -> bool:
    if not _docker_daemon_reachable():
        return False
    result = subprocess.run(["docker", "image", "inspect", IMAGE_REF], capture_output=True, timeout=10)
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _golden_image_built(),
    reason=(
        "requires a reachable Docker daemon and the python golden image built: "
        f"docker build -t {IMAGE_REF} components/languages/python"
    ),
)


@pytest.fixture
async def provisioner():
    p = DockerProvisioner()
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
            "print('hello from container')\n"
            "name = input()\n"
            "print('got:', name)\n"
        ),
        stdin="world",
        tenant_id="tenant-1",
        user_id=None,
        session=db_session,
    )

    assert result.exit_code == 0
    assert "hello from container" in result.stdout
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
    """The core correctness property from doc §5.1: a batch run never waits on a live
    client for stdin — a blocking read past the provided input sees EOF right away."""
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
