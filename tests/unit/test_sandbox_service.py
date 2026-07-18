from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import ComponentNotFoundError
from app.domain.execution import BatchRunResult
from app.extensions.loader import load_registry
from app.persistence.models import Run, Sandbox
from app.services.sandbox_service import SandboxService
from tests.unit.fakes import FakeProvisioner


@pytest.fixture
def registry():
    return load_registry()


async def test_execute_success_persists_run_and_destroys_sandbox(registry, db_session):
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(
            run_id="r1",
            exit_code=0,
            stdout="hi\n",
            stderr="",
            duration_ms=42,
            variables={"x": 1},
        )
    )
    service = SandboxService(registry, provisioner)

    result = await service.execute(
        language="python",
        code="x = 1\nprint('hi')",
        stdin="",
        tenant_id="tenant-1",
        user_id="user-1",
        session=db_session,
    )

    assert result.exit_code == 0
    assert result.variables == {"x": 1}
    assert len(provisioner.destroyed) == 1  # graceful eradication happened

    sandboxes = (await db_session.execute(select(Sandbox))).scalars().all()
    assert len(sandboxes) == 1
    assert sandboxes[0].state == "terminated"
    assert sandboxes[0].tenant_id == "tenant-1"

    runs = (await db_session.execute(select(Run))).scalars().all()
    assert len(runs) == 1
    assert runs[0].exit_code == 0
    assert runs[0].variables == {"x": 1}


async def test_execute_destroys_sandbox_even_when_exec_raises(registry, db_session):
    provisioner = FakeProvisioner(raise_on_exec=RuntimeError("container exploded"))
    service = SandboxService(registry, provisioner)

    with pytest.raises(RuntimeError, match="container exploded"):
        await service.execute(
            language="python",
            code="1/0",
            tenant_id="tenant-1",
            user_id=None,
            session=db_session,
        )

    # The whole point of the try/finally in SandboxService.execute: no leaked sandbox.
    assert len(provisioner.destroyed) == 1
    assert len(provisioner.acquired) == 1


async def test_batch_command_uses_batch_runner_entrypoint(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)

    await service.execute(
        language="python",
        code="print('hi')",
        stdin="fed input",
        tenant_id="tenant-1",
        user_id=None,
        session=db_session,
    )

    assert len(provisioner.exec_calls) == 1
    command = provisioner.exec_calls[0]
    assert command.command == ["python", "/opt/kubesandbox/runners/python_runner.py", "main.py"]
    assert command.capture_variables is True
    assert command.stdin == "fed input"
    assert command.files == {"main.py": "print('hi')"}
    assert command.timeout_seconds == 60
    assert command.max_output_bytes == 5_000_000


async def test_unknown_language_raises_component_not_found(registry, db_session):
    service = SandboxService(registry, FakeProvisioner())
    with pytest.raises(ComponentNotFoundError):
        await service.execute(
            language="cobol",
            code="IDENTIFICATION DIVISION.",
            tenant_id="tenant-1",
            user_id=None,
            session=db_session,
        )
