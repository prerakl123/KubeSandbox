"""Audit trail (doc §6 Layer 5: "Full **audit log** of every command/run (who, what,
when, exit code) in Postgres").

The `audit_logs` table has existed since Phase 0 with nothing writing to it, which made
that sentence in the design document false. This closes it.

**Why entries join the caller's transaction.** Every method here `session.add()`s to a
caller-supplied session and never commits. That makes the audit row atomic with the thing
it describes: an action that rolls back leaves no audit entry claiming it happened, and an
action that commits cannot commit without its entry. The usual "best-effort, never fail
the request" pattern is wrong for an audit log — an audit trail with silent holes in it is
worse than no audit trail, because it looks complete.

The two exceptions are events with no surrounding transaction to join — a login, a
rejected credential — which take their own session via `record_standalone()`.

**What is deliberately not recorded.** No request bodies, no code, no stdin, no stdout, no
credentials. `detail` carries identifiers, counts, and outcomes only. An audit log that
accumulates user source code becomes both a compliance liability and the most attractive
table in the database; doc §5's run records already hold output excerpts under the tenant's
own scoping, and that is the right place for them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.domain.auth import Principal
from app.persistence.models import AuditLog

logger = get_logger(__name__)

# Action vocabulary. A closed set of dotted `subject.verb` strings rather than free text,
# so the column is groupable and a query for "every destroy" can't miss entries spelled
# differently at different call sites.
SANDBOX_CREATE = "sandbox.create"
SANDBOX_DESTROY = "sandbox.destroy"
SANDBOX_RUN = "sandbox.run"
SANDBOX_ATTACH = "sandbox.attach"
SANDBOX_ATTACH_REJECTED = "sandbox.attach_rejected"
SANDBOX_FILE_READ = "sandbox.file_read"
SANDBOX_FILE_WRITE = "sandbox.file_write"

AUTH_LOGIN = "auth.login"
AUTH_LOGIN_FAILED = "auth.login_failed"

APIKEY_CREATE = "apikey.create"
APIKEY_REVOKE = "apikey.revoke"

ADMIN_ROLE_CHANGE = "admin.role_change"
ADMIN_ENTITLEMENT = "admin.entitlement"
ADMIN_PUBLISH_GRANT = "admin.publish_grant"
ADMIN_BILLING_MODE = "admin.billing_mode"
ADMIN_CREDIT_ADJUST = "admin.credit_adjust"
ADMIN_PRICING_RULE = "admin.pricing_rule"
ADMIN_CREDIT_REVIEW = "admin.credit_review"
ADMIN_QUOTA_CHANGE = "admin.quota_change"

COMPONENT_PUBLISH = "component.publish"
TEMPLATE_PUBLISH = "template.publish"
BUILD_TRIGGER = "build.trigger"

DENIED_QUOTA = "denied.quota"
DENIED_BILLING = "denied.billing"
DENIED_RATE_LIMIT = "denied.rate_limit"

_SERVICE_ACTOR_PREFIX = "service:"
_SYSTEM_ACTOR = "system"


def actor_for(principal: Principal | None) -> str:
    """Stable, non-empty actor string.

    A user id where there is one; `service:<tenant_id>` for an API-key caller, since a key
    authenticates as a tenant and not a person (doc §11) and recording a bare tenant id
    would be indistinguishable from a user id in the same column; `system` for the
    reconciler, whose TTL reaps and retention sweeps are real audited actions with no
    human behind them.
    """
    if principal is None:
        return _SYSTEM_ACTOR
    if principal.user_id:
        return principal.user_id
    return f"{_SERVICE_ACTOR_PREFIX}{principal.tenant_id}"


def actor_from_ids(tenant_id: str, user_id: str | None) -> str:
    """`actor_for` for the service layer, which threads `tenant_id`/`user_id` rather than a
    `Principal` (see `SandboxService`'s method signatures). Same output for the same
    identity, so an entry written from a router and one written from a service are
    attributable the same way."""
    return actor_for(Principal(tenant_id=tenant_id, user_id=user_id, role="user"))


class AuditService:
    """Config-only constructor, session passed per call — the same shape as
    `BillingService` and `WorkspaceService`, so one instance is reusable across request
    scopes."""

    def __init__(self, *, enabled: bool = True, session_factory: async_sessionmaker | None = None) -> None:
        self._enabled = enabled
        self._session_factory = session_factory
        """Only `record_standalone()` needs this — the events with no caller transaction
        to join. Everything else writes into the session it's handed."""

    def record(
        self,
        session: AsyncSession,
        *,
        action: str,
        principal: Principal | None = None,
        actor: str | None = None,
        tenant_id: str | None = None,
        target: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Queue an audit entry on `session`. Does not commit — the caller's own commit
        makes it atomic with the audited action.

        Synchronous on purpose: `session.add()` does no I/O, so making this `async` would
        add an await at every call site for nothing and imply a flush that isn't happening.
        """
        if not self._enabled:
            return
        session.add(
            AuditLog(
                tenant_id=tenant_id or (principal.tenant_id if principal else None),
                # `actor` wins when given — the service layer has ids rather than a
                # Principal, and falling through to `actor_for(None)` would attribute a
                # user's own run to `system`.
                actor=actor or actor_for(principal),
                action=action,
                target=target,
                detail=detail,
            )
        )

    async def record_standalone(
        self,
        *,
        action: str,
        principal: Principal | None = None,
        tenant_id: str | None = None,
        target: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """For events with no surrounding transaction — a login, a rejected credential, a
        rate-limit denial.

        This one *is* best-effort: it is called from paths that are already rejecting the
        request (or completing an authentication), and failing them a second time because
        the audit write failed would turn an observability problem into an outage. The
        failure is logged, so a vanished audit row is still visible somewhere.
        """
        if not self._enabled or self._session_factory is None:
            return
        try:
            async with self._session_factory() as session:
                self.record(
                    session,
                    action=action,
                    principal=principal,
                    tenant_id=tenant_id,
                    target=target,
                    detail=detail,
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — must never fail the calling path
            logger.warning("audit_write_failed", action=action, error=str(exc))

    @staticmethod
    def query(
        *,
        tenant_id: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        target: str | None = None,
    ):
        """Filtered SELECT for the admin read endpoint, returned as a statement so the API
        layer paginates it with the shared helper.

        Newest first with an `id` tiebreaker: entries written in one transaction share a
        `created_at` to the database's resolution, and without the tiebreaker paging over
        them can skip or repeat rows.
        """
        statement = select(AuditLog)
        if tenant_id is not None:
            statement = statement.where(AuditLog.tenant_id == tenant_id)
        if actor is not None:
            statement = statement.where(AuditLog.actor == actor)
        if action is not None:
            statement = statement.where(AuditLog.action == action)
        if target is not None:
            statement = statement.where(AuditLog.target == target)
        return statement.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
