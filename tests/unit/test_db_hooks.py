"""Unit tests for the concrete DB ComponentHooks (doc §3.5/§16, roadmap Phase 5) —
components/databases/{postgresql,mysql,redis}/hooks.py — against a minimal fake
standing in for Provisioner.exec_in. Whether the actual `psql`/`mysql`/`redis-cli`
invocations behave as expected against a real database is live-verification
territory (this project's established practice — see docs/TASK_CHECKLIST.md), not
something a fake can prove; these tests only prove each hook builds the commands it
promises to and reacts correctly to a nonzero exit code.
"""

from __future__ import annotations

import pytest

from app.core.errors import ProvisionerError
from app.domain.execution import BatchRunResult
from app.extensions.hooks import RenderContext
from app.services.credentials import DbCredentials
from tests.unit.factories import make_component


class FakeExecInProvisioner:
    def __init__(self, exit_code: int = 0) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self._exit_code = exit_code

    async def exec_in(self, sb, target, command, **kwargs) -> BatchRunResult:
        self.calls.append((target, command))
        return BatchRunResult(
            run_id="fake", exit_code=self._exit_code, stdout="", stderr="boom" if self._exit_code else "",
            duration_ms=1,
        )


def _credentials(**overrides) -> DbCredentials:
    defaults = dict(role="sandbox_user", password="pw123", database="sandbox", admin_password="adminpw")
    defaults.update(overrides)
    return DbCredentials(**defaults)


# -- postgresql ------------------------------------------------------------------------


async def test_postgresql_hook_issues_create_role_and_database():
    from components.databases.postgresql.hooks import hook as pg_hook

    component = make_component(
        "postgresql", "16.0", kind="sidecar", category="database", uid=999,
    )
    provisioner = FakeExecInProvisioner()
    ctx = RenderContext(component=component, credentials=_credentials(), provisioner=provisioner)

    await pg_hook.on_provision(sb=object(), ctx=ctx)

    assert [t for t, _ in provisioner.calls] == ["postgresql", "postgresql"]
    assert "CREATE ROLE sandbox_user WITH LOGIN PASSWORD 'pw123'" in provisioner.calls[0][1][-1]
    assert "CONNECTION LIMIT -1" in provisioner.calls[0][1][-1]
    assert "CREATE DATABASE sandbox OWNER sandbox_user" in provisioner.calls[1][1][-1]


async def test_postgresql_hook_raises_on_nonzero_exit():
    from components.databases.postgresql.hooks import hook as pg_hook

    component = make_component("postgresql", "16.0", kind="sidecar", category="database", uid=999)
    provisioner = FakeExecInProvisioner(exit_code=1)
    ctx = RenderContext(component=component, credentials=_credentials(), provisioner=provisioner)

    with pytest.raises(ProvisionerError, match="postgresql on_provision failed"):
        await pg_hook.on_provision(sb=object(), ctx=ctx)


# -- mysql -----------------------------------------------------------------------------


async def test_mysql_hook_issues_database_and_user_creation():
    from components.databases.mysql.hooks import hook as mysql_hook

    component = make_component("mysql", "8.4", kind="sidecar", category="database", uid=999)
    provisioner = FakeExecInProvisioner()
    ctx = RenderContext(component=component, credentials=_credentials(), provisioner=provisioner)

    await mysql_hook.on_provision(sb=object(), ctx=ctx)

    assert [t for t, _ in provisioner.calls] == ["mysql", "mysql"]
    first_command = provisioner.calls[0][1]
    assert first_command[0] == "sh"
    assert "adminpw" in first_command  # passed positionally, not embedded in the script
    assert "CREATE DATABASE IF NOT EXISTS `sandbox`" in first_command[-1]
    assert "CREATE USER IF NOT EXISTS 'sandbox_user'@'%'" in provisioner.calls[1][1][-1]


async def test_mysql_hook_raises_on_nonzero_exit():
    from components.databases.mysql.hooks import hook as mysql_hook

    component = make_component("mysql", "8.4", kind="sidecar", category="database", uid=999)
    provisioner = FakeExecInProvisioner(exit_code=1)
    ctx = RenderContext(component=component, credentials=_credentials(), provisioner=provisioner)

    with pytest.raises(ProvisionerError, match="mysql on_provision failed"):
        await mysql_hook.on_provision(sb=object(), ctx=ctx)


# -- redis -----------------------------------------------------------------------------


async def test_redis_hook_sets_acl_user_then_disables_default():
    from components.databases.redis.hooks import hook as redis_hook

    component = make_component("redis", "7.0", kind="sidecar", category="database", uid=999)
    provisioner = FakeExecInProvisioner()
    ctx = RenderContext(component=component, credentials=_credentials(), provisioner=provisioner)

    await redis_hook.on_provision(sb=object(), ctx=ctx)

    assert [t for t, _ in provisioner.calls] == ["redis", "redis"]
    setuser_command = provisioner.calls[0][1]
    assert setuser_command == ["redis-cli", "ACL", "SETUSER", "sandbox_user", "on", ">pw123", "~*", "+@all", "-@dangerous"]
    assert provisioner.calls[1][1] == ["redis-cli", "ACL", "SETUSER", "default", "off"]


async def test_redis_hook_raises_on_nonzero_exit():
    from components.databases.redis.hooks import hook as redis_hook

    component = make_component("redis", "7.0", kind="sidecar", category="database", uid=999)
    provisioner = FakeExecInProvisioner(exit_code=1)
    ctx = RenderContext(component=component, credentials=_credentials(), provisioner=provisioner)

    with pytest.raises(ProvisionerError, match="redis on_provision failed"):
        await redis_hook.on_provision(sb=object(), ctx=ctx)
