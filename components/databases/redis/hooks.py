"""ComponentHook for the `redis` sidecar (doc §3.5, §16, roadmap Phase 5) — creates the
scoped ACL user that main's DATABASE_URL env var already promised, once the sidecar
container is healthy, then locks the default user out.

Redis has no SQL-style role/grant model, so the "no superuser" principle from doc §16
is applied via Redis' own ACL SETUSER mechanism instead of a GRANT statement — the
`access.database.grants` list in component.yaml is Redis' own ACL rule vocabulary
(`~*`, `+@all`, `-@dangerous`, ...), applied here verbatim rather than translated from
a generic cross-database grants vocabulary the way the postgresql/mysql hooks do.

Runs `redis-cli` unauthenticated — the official redis image has no env-based bootstrap
password (unlike Postgres/MySQL's adminPasswordEnv), so it starts with the default
user open by default; that's precisely what lets this hook connect and configure the
scoped user in the first place. The default user is disabled immediately afterward, so
by the time on_provision returns, the only way in is the scoped credentials.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ProvisionerError
from app.domain.execution import SandboxHandle
from app.extensions.hooks import RenderContext

_TARGET = "redis"
_EXEC_TIMEOUT_SECONDS = 30
_DEFAULT_ACL_RULES = ["~*", "+@all", "-@dangerous"]


class RedisHook:
    async def validate(self, ctx: Any) -> None:
        pass

    async def mutate_pod_spec(self, spec: Any, ctx: RenderContext) -> Any:
        return spec

    async def on_provision(self, sb: SandboxHandle, ctx: RenderContext) -> None:
        credentials = ctx.credentials
        access = ctx.component.spec.access.database
        rules = access.grants if access and access.grants else _DEFAULT_ACL_RULES

        await self._redis_cli(
            ctx, sb, ["ACL", "SETUSER", credentials.role, "on", f">{credentials.password}", *rules]
        )
        # Only now, with the scoped user confirmed configured, close the door it came
        # in through — Redis' own bootstrap identity here is the unauthenticated
        # default user, not an admin password (see module docstring).
        await self._redis_cli(ctx, sb, ["ACL", "SETUSER", "default", "off"])

    async def on_teardown(self, sb: SandboxHandle) -> None:
        # No-op: whole-sandbox teardown destroys this sidecar container outright.
        pass

    async def _redis_cli(self, ctx: RenderContext, sb: SandboxHandle, args: list[str]) -> None:
        result = await ctx.provisioner.exec_in(
            sb, _TARGET, ["redis-cli", *args], timeout_seconds=_EXEC_TIMEOUT_SECONDS
        )
        if result.exit_code:
            raise ProvisionerError(f"redis on_provision failed: {result.stderr or result.stdout}")


hook = RedisHook()
