from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import SandboxNotFoundError
from app.domain.execution import BatchRunResult, SandboxState
from app.extensions.loader import load_registry
from app.persistence.models import Run, Sandbox
from app.services import sandbox_service as sandbox_service_module
from app.services.sandbox_service import SandboxService
from tests.unit.fakes import FakeProvisioner


@pytest.fixture
def registry():
    return load_registry()


async def test_create_sandbox_persists_active_row_and_does_not_destroy(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)

    row = await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id="user-1", session=db_session
    )

    assert row.state == "active"
    assert row.component_refs == ["python@3.12.4"]
    assert provisioner.destroyed == []  # unlike execute(), never torn down here

    rows = (await db_session.execute(select(Sandbox))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == row.id


async def test_get_sandbox_raises_not_found_for_wrong_tenant(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)
    row = await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id=None, session=db_session
    )

    with pytest.raises(SandboxNotFoundError):
        await service.get_sandbox(row.id, "tenant-2", db_session)


async def test_get_sandbox_status_self_heals_when_provisioner_reports_gone(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)
    row = await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id=None, session=db_session
    )

    async def _gone(handle):
        from app.domain.execution import SandboxStatus

        return SandboxStatus(sandbox_id=handle.sandbox_id, state=SandboxState.TERMINATED)

    provisioner.status = _gone  # type: ignore[method-assign]

    refreshed_row, status = await service.get_sandbox_status(row.id, "tenant-1", db_session)
    assert status.state == SandboxState.TERMINATED
    assert refreshed_row.state == "terminated"
    assert refreshed_row.terminated_at is not None


async def test_destroy_sandbox_is_idempotent(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)
    row = await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id=None, session=db_session
    )

    await service.destroy_sandbox(row.id, "tenant-1", db_session)
    assert len(provisioner.destroyed) == 1

    await service.destroy_sandbox(row.id, "tenant-1", db_session)  # must not raise or re-destroy
    assert len(provisioner.destroyed) == 1


async def test_run_in_sandbox_does_not_destroy_and_persists_run(registry, db_session):
    provisioner = FakeProvisioner(
        batch_result=BatchRunResult(run_id="r1", exit_code=0, stdout="hi\n", stderr="", duration_ms=5)
    )
    service = SandboxService(registry, provisioner)
    row = await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id=None, session=db_session
    )

    result = await service.run_in_sandbox(
        row.id, code="print('hi')", tenant_id="tenant-1", session=db_session
    )

    assert result.exit_code == 0
    assert provisioner.destroyed == []
    runs = (await db_session.execute(select(Run))).scalars().all()
    assert len(runs) == 1
    assert runs[0].sandbox_id == row.id


async def test_run_in_terminated_sandbox_raises_not_found(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)
    row = await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id=None, session=db_session
    )
    await service.destroy_sandbox(row.id, "tenant-1", db_session)

    with pytest.raises(SandboxNotFoundError):
        await service.run_in_sandbox(row.id, code="1", tenant_id="tenant-1", session=db_session)


# -- DB sidecar composition + ComponentHook wiring (doc §3.5, roadmap Phase 5) --------


async def test_create_sandbox_with_db_template_persists_sidecar_refs(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)

    row = await service.create_sandbox(
        language="python", template="python-postgres-lab@1.0",
        tenant_id="tenant-1", user_id=None, session=db_session,
    )

    assert row.sidecar_refs == {"postgresql": "fake-sidecar-postgresql"}


async def test_create_sandbox_with_db_template_runs_real_hook_via_exec_in(registry, db_session):
    """No monkeypatching of the hook itself here — this exercises the real
    components/databases/postgresql/hooks.py module end-to-end (loader -> on_provision
    -> exec_in), just against a FakeProvisioner instead of a live Postgres."""
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)

    await service.create_sandbox(
        language="python", template="python-postgres-lab@1.0",
        tenant_id="tenant-1", user_id=None, session=db_session,
    )

    targets = [target for target, _command in provisioner.exec_in_calls]
    # CREATE ROLE, CREATE DATABASE, then GRANT + ALTER DATABASE statement_timeout
    assert targets == ["postgresql", "postgresql", "postgresql"]
    create_role_sql = provisioner.exec_in_calls[0][1][-1]
    assert "CREATE ROLE sandbox_user" in create_role_sql
    assert "NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION" in create_role_sql
    grant_sql = provisioner.exec_in_calls[2][1][-1]
    assert "GRANT CONNECT, CREATE, TEMPORARY ON DATABASE sandbox TO sandbox_user" in grant_sql
    assert "statement_timeout = '30s'" in grant_sql


async def test_create_sandbox_with_mysql_template_runs_real_hook_via_exec_in(registry, db_session):
    """Same reasoning as the postgresql test above — real
    components/databases/mysql/hooks.py, fake provisioner."""
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)

    await service.create_sandbox(
        language="python", template="python-mysql-lab@1.0",
        tenant_id="tenant-1", user_id=None, session=db_session,
    )

    targets = [target for target, _command in provisioner.exec_in_calls]
    # CREATE DATABASE, CREATE USER, GRANT, then SET GLOBAL max_execution_time
    assert targets == ["mysql", "mysql", "mysql", "mysql"]
    create_db_sql = provisioner.exec_in_calls[0][1][-1]
    assert "CREATE DATABASE IF NOT EXISTS `sandbox`" in create_db_sql
    create_user_sql = provisioner.exec_in_calls[1][1][-1]
    assert "CREATE USER IF NOT EXISTS 'sandbox_user'@'%'" in create_user_sql
    assert "WITH MAX_USER_CONNECTIONS 10" in create_user_sql
    grant_sql = provisioner.exec_in_calls[2][1][-1]
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in grant_sql
    assert "ON `sandbox`.* TO 'sandbox_user'@'%'" in grant_sql
    timeout_sql = provisioner.exec_in_calls[3][1][-1]
    assert "SET GLOBAL max_execution_time = 30000" in timeout_sql


async def test_create_sandbox_with_redis_template_runs_real_hook_via_exec_in(registry, db_session):
    """Same reasoning as the postgresql test above — real
    components/databases/redis/hooks.py, fake provisioner."""
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)

    await service.create_sandbox(
        language="python", template="python-redis-lab@1.0",
        tenant_id="tenant-1", user_id=None, session=db_session,
    )

    targets = [target for target, _command in provisioner.exec_in_calls]
    assert targets == ["redis", "redis"]
    setuser_command = provisioner.exec_in_calls[0][1]
    assert setuser_command[:4] == ["redis-cli", "ACL", "SETUSER", "sandbox_user"]
    assert "~*" in setuser_command and "+@all" in setuser_command and "-@dangerous" in setuser_command
    assert provisioner.exec_in_calls[1][1] == ["redis-cli", "ACL", "SETUSER", "default", "off"]


async def test_create_sandbox_without_sidecars_never_calls_exec_in(registry, db_session):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)

    await service.create_sandbox(
        language="python", tenant_id="tenant-1", user_id=None, session=db_session
    )

    assert provisioner.exec_in_calls == []


async def test_destroy_sandbox_calls_sidecar_on_teardown_hook(registry, db_session, monkeypatch):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)

    torn_down: list[str] = []

    class _RecordingHook:
        async def on_provision(self, sb, ctx) -> None:
            pass

        async def on_teardown(self, sb) -> None:
            torn_down.append(sb.sandbox_id)

    monkeypatch.setattr(sandbox_service_module, "load_hook", lambda module_path: _RecordingHook())

    row = await service.create_sandbox(
        language="python", template="python-postgres-lab@1.0",
        tenant_id="tenant-1", user_id=None, session=db_session,
    )
    await service.destroy_sandbox(row.id, "tenant-1", db_session)

    assert torn_down == [row.id]


async def test_destroy_sandbox_survives_a_failing_teardown_hook(registry, db_session, monkeypatch):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)

    class _BrokenHook:
        async def on_provision(self, sb, ctx) -> None:
            pass

        async def on_teardown(self, sb) -> None:
            raise RuntimeError("sidecar unreachable")

    monkeypatch.setattr(sandbox_service_module, "load_hook", lambda module_path: _BrokenHook())

    row = await service.create_sandbox(
        language="python", template="python-postgres-lab@1.0",
        tenant_id="tenant-1", user_id=None, session=db_session,
    )

    await service.destroy_sandbox(row.id, "tenant-1", db_session)  # must not raise
    assert provisioner.destroyed == [row.id]


async def test_create_sandbox_destroys_handle_when_sidecar_provisioning_fails(
    registry, db_session, monkeypatch
):
    """A create_sandbox() failure before the row is persisted must not leak a running
    container/pod — the caller has no id to destroy it with afterward."""
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)

    class _BrokenHook:
        async def on_provision(self, sb, ctx) -> None:
            raise RuntimeError("sidecar unreachable")

        async def on_teardown(self, sb) -> None:
            pass

    monkeypatch.setattr(sandbox_service_module, "load_hook", lambda module_path: _BrokenHook())

    with pytest.raises(RuntimeError, match="sidecar unreachable"):
        await service.create_sandbox(
            language="python", template="python-postgres-lab@1.0",
            tenant_id="tenant-1", user_id=None, session=db_session,
        )

    assert len(provisioner.destroyed) == 1
    rows = (await db_session.execute(select(Sandbox))).scalars().all()
    assert rows == []  # never persisted — nothing for the caller to clean up later


async def test_execute_destroys_and_still_tears_down_when_sidecar_provisioning_fails(
    registry, db_session, monkeypatch
):
    provisioner = FakeProvisioner()
    service = SandboxService(registry, provisioner)

    torn_down: list[str] = []

    class _BrokenHook:
        async def on_provision(self, sb, ctx) -> None:
            raise RuntimeError("sidecar unreachable")

        async def on_teardown(self, sb) -> None:
            torn_down.append(sb.sandbox_id)

    monkeypatch.setattr(sandbox_service_module, "load_hook", lambda module_path: _BrokenHook())

    with pytest.raises(RuntimeError, match="sidecar unreachable"):
        await service.execute(
            language="python", code="print(1)", template="python-postgres-lab@1.0",
            tenant_id="tenant-1", user_id=None, session=db_session,
        )

    assert len(provisioner.destroyed) == 1
    assert len(torn_down) == 1
