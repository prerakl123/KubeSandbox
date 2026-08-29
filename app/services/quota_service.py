"""QuotaService — per-tenant resource ceilings (doc §11, doc §10.1's `quotas` table).

Doc §11 says quotas are "enforced by `QuotaService`/`BillingService` before create". Only
the `BillingService` half existed; the `quotas` table doc §10.1 lists had never even been
created, so a tenant could occupy the entire cluster as long as its wallet held out. This
closes that.

**Quotas and billing answer different questions**, which is why both exist:

* Billing asks "can this tenant *afford* this?" — consumable, and a funded tenant can keep
  going indefinitely.
* Quotas ask "should this tenant be *allowed* this much at once?" — a ceiling, and it
  binds regardless of funding. It is what protects the cluster from a single tenant, and
  what makes a free tier possible at all.

A tenant with billing disabled (the default in both env profiles) has no spend limit
whatsoever, so without quotas there is currently nothing at all bounding concurrency.

**Enforcement is check-then-act, not a reservation**, and that is a real limitation:
between the count and the insert, a concurrent request can push a tenant one sandbox over
its cap. Bounded (over-admission is at most the number of simultaneous in-flight creates)
and deliberate — a hard guarantee needs either `SELECT ... FOR UPDATE` on the quota row,
serializing every create for a tenant, or a reservation table with its own reaper for
crashed reservations. Neither is worth it for a limit whose purpose is stopping a tenant
from taking hundreds of sandboxes, not exactly N. Documented rather than papered over.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import QuotaExceededError
from app.core.logging import get_logger
from app.domain.execution import ResourceSpec
from app.persistence.models import Quota, Run, Sandbox
from app.provisioners.resources import parse_cpu_to_nanocpus, parse_memory_to_bytes

logger = get_logger(__name__)

_NANOCPUS_PER_MILLICORE = 1_000_000
_BYTES_PER_MB = 1024 * 1024
_TERMINAL_STATES = ("terminated", "failed")
"""What doesn't count against a concurrency quota. `failed` is included deliberately: a
sandbox that failed to provision holds no resources, and counting it would let a run of
provisioning failures lock a tenant out of the platform entirely."""


@dataclass(frozen=True)
class QuotaUsage:
    """A tenant's current position against each configured ceiling.

    Serves both enforcement and the self-service read endpoint — a UI showing "3 of 10
    sandboxes" must be looking at exactly the numbers enforcement uses, or the two
    disagree and the user is told they have headroom they don't.
    """

    concurrent_sandboxes: int
    cpu_millicores: int
    memory_mb: int
    monthly_minutes: int

    max_concurrent_sandboxes: int | None
    max_cpu_millicores: int | None
    max_memory_mb: int | None
    max_monthly_minutes: int | None


class QuotaService:
    """Config-only constructor, session per call — the `BillingService` shape."""

    def __init__(
        self,
        *,
        default_max_concurrent_sandboxes: int | None = None,
        default_max_cpu_millicores: int | None = None,
        default_max_memory_mb: int | None = None,
        default_max_monthly_minutes: int | None = None,
    ) -> None:
        self._defaults = {
            "max_concurrent_sandboxes": default_max_concurrent_sandboxes,
            "max_cpu_millicores": default_max_cpu_millicores,
            "max_memory_mb": default_max_memory_mb,
            "max_monthly_minutes": default_max_monthly_minutes,
        }

    async def get_or_create(self, tenant_id: str, *, session: AsyncSession) -> Quota:
        """Lazily materialize a tenant's quota row from the configured defaults — the same
        pattern `BillingService._get_or_create_account` and `WorkspaceService.get_or_create`
        follow, so an operator never has to pre-create rows for a tenant to work."""
        row = await session.get(Quota, tenant_id)
        if row is None:
            row = Quota(tenant_id=tenant_id, **self._defaults)
            session.add(row)
            await session.flush()
        return row

    async def usage(self, tenant_id: str, *, session: AsyncSession) -> QuotaUsage:
        quota = await self.get_or_create(tenant_id, session=session)

        live = (
            (
                await session.execute(
                    select(Sandbox).where(
                        Sandbox.tenant_id == tenant_id,
                        Sandbox.state.not_in(_TERMINAL_STATES),
                    )
                )
            )
            .scalars()
            .all()
        )

        # Only computed when a corresponding ceiling is configured. Re-deriving a
        # `ResourceSpec` per sandbox means resolving its component/template (see
        # SandboxService._spec_resources_for_row), which is not free — and pointless work
        # for a deployment that only caps concurrency.
        cpu_millicores = 0
        memory_mb = 0
        if quota.max_cpu_millicores is not None or quota.max_memory_mb is not None:
            for row in live:
                cpu_millicores += _millicores_from_row(row)
                memory_mb += _memory_mb_from_row(row)

        monthly_minutes = 0
        if quota.max_monthly_minutes is not None:
            monthly_minutes = await self._monthly_minutes(tenant_id, session=session)

        return QuotaUsage(
            concurrent_sandboxes=len(live),
            cpu_millicores=cpu_millicores,
            memory_mb=memory_mb,
            monthly_minutes=monthly_minutes,
            max_concurrent_sandboxes=quota.max_concurrent_sandboxes,
            max_cpu_millicores=quota.max_cpu_millicores,
            max_memory_mb=quota.max_memory_mb,
            max_monthly_minutes=quota.max_monthly_minutes,
        )

    async def _monthly_minutes(self, tenant_id: str, *, session: AsyncSession) -> int:
        """Sandbox minutes this calendar month.

        Sums `runs.duration_ms` — the only durable record of how long compute actually
        ran. Non-ephemeral sandbox *lifetime* is not included: it is billed at destroy
        time (Phase 8) but isn't attributable to a `runs` row, and double-counting a warm
        sandbox's runs plus its wall-clock lifetime would overstate usage badly. Honest
        consequence: an idle long-lived sandbox consumes little monthly-minute quota, so
        this dimension caps *compute*, not occupancy. Concurrency caps occupancy.
        """
        month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total_ms = (
            await session.execute(
                select(func.coalesce(func.sum(Run.duration_ms), 0)).where(
                    Run.tenant_id == tenant_id, Run.created_at >= month_start
                )
            )
        ).scalar_one()
        return int(total_ms or 0) // 60_000

    async def check(
        self,
        tenant_id: str,
        *,
        resources: ResourceSpec | None = None,
        session: AsyncSession,
    ) -> None:
        """Raise `QuotaExceededError` (429) if creating one more sandbox would breach a
        ceiling. A no-op for every dimension left unset.

        `resources` is the pending sandbox's own limits, added to current usage before
        comparing — checking usage alone would let a single enormous sandbox through
        whenever the tenant happened to be at zero.
        """
        usage = await self.usage(tenant_id, session=session)

        if (
            usage.max_concurrent_sandboxes is not None
            and usage.concurrent_sandboxes + 1 > usage.max_concurrent_sandboxes
        ):
            raise QuotaExceededError(
                f"concurrent sandbox quota exceeded: {usage.concurrent_sandboxes} live, "
                f"limit {usage.max_concurrent_sandboxes}"
            )

        pending_cpu = _millicores_from_spec(resources) if resources else 0
        if (
            usage.max_cpu_millicores is not None
            and usage.cpu_millicores + pending_cpu > usage.max_cpu_millicores
        ):
            raise QuotaExceededError(
                f"cpu quota exceeded: {usage.cpu_millicores}m in use + {pending_cpu}m requested, "
                f"limit {usage.max_cpu_millicores}m"
            )

        pending_memory = _memory_mb_from_spec(resources) if resources else 0
        if usage.max_memory_mb is not None and usage.memory_mb + pending_memory > usage.max_memory_mb:
            raise QuotaExceededError(
                f"memory quota exceeded: {usage.memory_mb}MiB in use + {pending_memory}MiB "
                f"requested, limit {usage.max_memory_mb}MiB"
            )

        if (
            usage.max_monthly_minutes is not None
            and usage.monthly_minutes >= usage.max_monthly_minutes
        ):
            # `>=` rather than `>`: unlike the others this isn't a capacity check with a
            # known increment — the pending run's duration is unknown until it finishes —
            # so the cap is enforced as "already at the limit, no new work".
            raise QuotaExceededError(
                f"monthly minute quota exhausted: {usage.monthly_minutes} of "
                f"{usage.max_monthly_minutes} minutes used this month"
            )

    async def set_quota(
        self,
        tenant_id: str,
        *,
        session: AsyncSession,
        max_concurrent_sandboxes: int | None = None,
        max_cpu_millicores: int | None = None,
        max_memory_mb: int | None = None,
        max_monthly_minutes: int | None = None,
        clear_unset: bool = False,
    ) -> Quota:
        """Admin update.

        `clear_unset` distinguishes the two things a `None` can mean over HTTP: by default
        an omitted field leaves that dimension alone (a PATCH), and with `clear_unset` an
        omitted field explicitly removes the limit (a PUT). Without this an admin has no
        way to *remove* a cap, because "unset" and "no limit" would be indistinguishable.
        """
        quota = await self.get_or_create(tenant_id, session=session)
        updates = {
            "max_concurrent_sandboxes": max_concurrent_sandboxes,
            "max_cpu_millicores": max_cpu_millicores,
            "max_memory_mb": max_memory_mb,
            "max_monthly_minutes": max_monthly_minutes,
        }
        for field, value in updates.items():
            if value is not None or clear_unset:
                setattr(quota, field, value)
        await session.commit()
        await session.refresh(quota)
        logger.info("quota_updated", tenant_id=tenant_id, **updates, clear_unset=clear_unset)
        return quota


# -- resource accounting -------------------------------------------------------------
# Sandbox rows don't store resolved cpu/memory, so a live sandbox's contribution has to
# come from somewhere. Re-resolving each one's component/template on every quota check
# would mean registry lookups inside a request that is only trying to count — so these
# read the `weight_class` the row *does* store and use the per-class budget below.


_WEIGHT_CLASS_BUDGET: dict[str, tuple[int, int]] = {
    "light": (500, 512),
    "standard": (1000, 1024),
    "heavy": (2000, 4096),
}
"""(millicores, MiB) charged against quota per live sandbox, by weight class.

An approximation, and flagged as one: the authoritative numbers are each component's own
`runtime.resources.limits`, which vary per component and per template override. These are
the representative budgets for the three classes doc §4.3 defines, chosen to be
*conservative* (at or above what a typical component in that class declares) so quota
enforcement errs toward refusing rather than over-admitting.

The exact-accounting alternative is to persist resolved cpu/memory on the `sandboxes` row
at create time, which is the right long-term fix and a schema change this pass isn't
taking. Recorded in the checklist's scope boundaries.
"""

_DEFAULT_BUDGET = _WEIGHT_CLASS_BUDGET["standard"]


def _millicores_from_row(row: Sandbox) -> int:
    return _WEIGHT_CLASS_BUDGET.get(row.weight_class, _DEFAULT_BUDGET)[0]


def _memory_mb_from_row(row: Sandbox) -> int:
    return _WEIGHT_CLASS_BUDGET.get(row.weight_class, _DEFAULT_BUDGET)[1]


def _millicores_from_spec(resources: ResourceSpec) -> int:
    return parse_cpu_to_nanocpus(resources.cpu) // _NANOCPUS_PER_MILLICORE


def _memory_mb_from_spec(resources: ResourceSpec) -> int:
    return parse_memory_to_bytes(resources.memory) // _BYTES_PER_MB
