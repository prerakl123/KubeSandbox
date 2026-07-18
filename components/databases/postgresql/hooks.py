"""ComponentHook for the `postgresql` sidecar (doc §3.5, §16, roadmap Phase 5) —
creates the scoped, non-superuser role and per-sandbox database that main's
DATABASE_URL env var already promised, once the sidecar container is healthy.

Runs `psql` as the sidecar's own bootstrap "postgres" superuser over the local unix
socket (exec_in runs inside the sidecar container itself). The official postgres image
trusts local (unix-socket) connections without a password by default, so there's no
need to plumb POSTGRES_PASSWORD through to psql at all — only the DB's own entrypoint
needs it, to set the postgres superuser's password on first boot.

CREATE ROLE and CREATE DATABASE are issued as separate psql invocations: Postgres
forbids CREATE DATABASE inside a transaction block, and a single `-c` string with
multiple `;`-separated statements risks exactly that depending on how the client
frames it — separate invocations sidestep the question entirely.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ProvisionerError
from app.domain.execution import SandboxHandle
from app.extensions.hooks import RenderContext

_TARGET = "postgresql"
_ADMIN_USER = "postgres"
_ADMIN_DB = "postgres"
_EXEC_TIMEOUT_SECONDS = 30


class PostgresHook:
    async def validate(self, ctx: Any) -> None:
        pass

    async def mutate_pod_spec(self, spec: Any, ctx: RenderContext) -> Any:
        return spec

    async def on_provision(self, sb: SandboxHandle, ctx: RenderContext) -> None:
        credentials = ctx.credentials
        access = ctx.component.spec.access.database
        limits = access.limits if access else None

        conn_limit = limits.maxConnections if limits and limits.maxConnections else -1
        await self._psql(
            ctx,
            sb,
            f"CREATE ROLE {credentials.role} WITH LOGIN PASSWORD '{credentials.password}' "
            f"NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION CONNECTION LIMIT {conn_limit};",
        )
        await self._psql(ctx, sb, f"CREATE DATABASE {credentials.database} OWNER {credentials.role};")

        grants = access.grants if access else []
        statements = []
        if grants:
            statements.append(
                f"GRANT {', '.join(grants)} ON DATABASE {credentials.database} TO {credentials.role};"
            )
        if limits and limits.statementTimeout:
            statements.append(
                f"ALTER DATABASE {credentials.database} SET statement_timeout = "
                f"'{limits.statementTimeout}';"
            )
        # maxDbSizeMB is declared in the manifest but NOT enforced here — Postgres has
        # no native per-database size cap; enforcing it needs an extension (e.g.
        # pg_quota) or an external reconciler polling pg_database_size(), which is out
        # of scope for Phase 5. Documented gap, not a silent one — see
        # docs/TASK_CHECKLIST.md's Phase 5 "Known scope boundaries".
        if statements:
            await self._psql(ctx, sb, " ".join(statements))

    async def on_teardown(self, sb: SandboxHandle) -> None:
        # No-op: whole-sandbox teardown destroys this sidecar container outright
        # (an ephemeral tmpfs/emptyDir data dir, nothing persists past it anyway).
        pass

    async def _psql(self, ctx: RenderContext, sb: SandboxHandle, sql: str) -> None:
        result = await ctx.provisioner.exec_in(
            sb,
            _TARGET,
            ["psql", "-U", _ADMIN_USER, "-d", _ADMIN_DB, "-v", "ON_ERROR_STOP=1", "-c", sql],
            timeout_seconds=_EXEC_TIMEOUT_SECONDS,
        )
        if result.exit_code:
            raise ProvisionerError(f"postgresql on_provision failed: {result.stderr or result.stdout}")


hook = PostgresHook()
