"""ComponentHook for the `mysql` sidecar (doc §3.5, §16, roadmap Phase 5) — creates the
scoped, non-FILE/SUPER/PROCESS user and per-sandbox database that main's DATABASE_URL
env var already promised, once the sidecar container is healthy.

Runs the `mysql` CLI as root, authenticated with the sidecar's own bootstrap
MYSQL_ROOT_PASSWORD (injected via adminPasswordEnv). Unlike Postgres, the official
MySQL image's root user always requires a password — even over the local unix socket —
so it has to be passed through here. Passed via a per-exec `MYSQL_PWD` env var (not the
more common but equally-visible `-p<password>` CLI form); visible to anyone who can
inspect this specific sidecar container's process list, which today is only this
control plane's own admin-exec path, never the sandboxed end user.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ProvisionerError
from app.domain.execution import SandboxHandle
from app.extensions.hooks import RenderContext

_TARGET = "mysql"
_EXEC_TIMEOUT_SECONDS = 30


def _parse_seconds(duration: str) -> int:
    """"30s" -> 30 — the only shorthand the manifests actually use; kept this minimal
    on purpose (see module docstring's scope)."""
    duration = duration.strip()
    if duration.endswith("s"):
        return int(duration[:-1])
    return int(duration)


class MySQLHook:
    async def validate(self, ctx: Any) -> None:
        pass

    async def mutate_pod_spec(self, spec: Any, ctx: RenderContext) -> Any:
        return spec

    async def on_provision(self, sb: SandboxHandle, ctx: RenderContext) -> None:
        credentials = ctx.credentials
        access = ctx.component.spec.access.database
        limits = access.limits if access else None
        admin_password = credentials.admin_password

        await self._mysql(ctx, sb, admin_password, f"CREATE DATABASE IF NOT EXISTS `{credentials.database}`;")

        conn_limit_clause = ""
        if limits and limits.maxConnections:
            conn_limit_clause = f" WITH MAX_USER_CONNECTIONS {limits.maxConnections}"
        await self._mysql(
            ctx,
            sb,
            admin_password,
            f"CREATE USER IF NOT EXISTS '{credentials.role}'@'%' IDENTIFIED BY "
            f"'{credentials.password}'{conn_limit_clause};",
        )

        grants = access.grants if access else []
        if grants:
            await self._mysql(
                ctx,
                sb,
                admin_password,
                f"GRANT {', '.join(grants)} ON `{credentials.database}`.* TO "
                f"'{credentials.role}'@'%'; FLUSH PRIVILEGES;",
            )

        # Single-tenant sidecar (this mysqld instance exists for exactly one
        # sandbox), so a GLOBAL setting only ever affects this one sandbox's private
        # database — the closest MySQL equivalent to Postgres' per-role
        # statement_timeout, since MAX_EXECUTION_TIME has no persistent per-user form,
        # only a session variable. maxDbSizeMB is declared but NOT enforced — same
        # documented gap as the postgresql hook (see docs/TASK_CHECKLIST.md's Phase 5
        # "Known scope boundaries").
        if limits and limits.statementTimeout:
            ms = _parse_seconds(limits.statementTimeout) * 1000
            await self._mysql(ctx, sb, admin_password, f"SET GLOBAL max_execution_time = {ms};")

    async def on_teardown(self, sb: SandboxHandle) -> None:
        # No-op: whole-sandbox teardown destroys this sidecar container outright.
        pass

    async def _mysql(self, ctx: RenderContext, sb: SandboxHandle, admin_password: str, sql: str) -> None:
        result = await ctx.provisioner.exec_in(
            sb,
            _TARGET,
            ["sh", "-c", 'MYSQL_PWD="$1" mysql -uroot -e "$2"', "--", admin_password, sql],
            timeout_seconds=_EXEC_TIMEOUT_SECONDS,
        )
        if result.exit_code:
            raise ProvisionerError(f"mysql on_provision failed: {result.stderr or result.stdout}")


hook = MySQLHook()
