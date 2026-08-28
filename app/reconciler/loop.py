"""The reconciler (doc §4.1, §20 Phase 7) — a dedicated worker process, not an
in-API background task (doc's own wording: "a dedicated worker; upgradeable to a
`kopf` operator in aks-prod"). Runs a fixed-interval tick doing five independent jobs:

1. TTL reaping — destroys any non-terminated Sandbox past its `idle_ttl_seconds`
   (no activity since `last_active_at`, or `created_at` if it never ran anything) or
   `max_ttl_seconds` (age since `created_at`, regardless of activity). Routes through
   `SandboxService.destroy_sandbox()`, which also bills a non-ephemeral sandbox's
   real lifetime usage when billing is enabled (doc §13, Phase 8) — no separate
   wiring needed here for that.
2. Pool replenishment — tops up each poolable language component's warm pool to its
   configured target count (doc §4.3). No-op when `pool.enabled` is false.
3. Workspace retention sweep — archives/purges persistent workspaces per doc §10.2.
   No-op when `workspace.persistence_enabled` is false.
4. Workspace storage billing (doc §13/§10.1's `storage_gb_day`) — prices each active
   workspace's last-measured `used_mb` against the tick interval as GB-days. No-op
   when `billing.enabled` is false.
5. Orphan GC — removes provisioner-native resources (a Docker container, a
   Kubernetes namespace) carrying a `sandbox-id` label with no matching, non-
   terminated `Sandbox` row — a crash between `acquire()` and its row being
   committed, or a control-plane restart mid-`destroy()`, could otherwise leak one
   forever.

Constructs its own Provisioner/Registry/session-factory/ObjectStorageProvider via the
same `app.core.bootstrap` functions `app/main.py`'s lifespan uses — this is a
genuinely separate process sharing no in-memory state with any API replica, only the
database (doc §2's "any replica can serve any session" applies here too: the
reconciler doesn't care which API replica created a sandbox, only what Postgres says).

Run standalone: `uv run python -m app.reconciler.loop`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cloud.storage import ObjectStorageProvider
from app.core.bootstrap import build_object_storage_provider, build_provisioner, validate_cloud_providers
from app.core.config import Settings, get_settings
from app.core.errors import ComponentNotFoundError, KubeSandboxError
from app.core.logging import configure_logging, get_logger
from app.core.tracing import configure_tracing
from app.domain.billing import UsageEvent
from app.domain.execution import SandboxHandle
from app.extensions.loader import Registry, load_registry
from app.persistence.db import get_session_factory
from app.persistence.models import Sandbox, User, Workspace
from app.provisioners.base import Provisioner
from app.services.billing_service import BillingService
from app.services.pool_manager import PoolManager
from app.services.sandbox_service import SandboxService, build_ad_hoc_spec
from app.services.workspace_service import WorkspaceService

logger = get_logger(__name__)


@dataclass
class ReconcileTickResult:
    reaped: list[str] = field(default_factory=list)
    pool_replenished: dict[str, int] = field(default_factory=dict)
    workspaces_archived: list[str] = field(default_factory=list)
    workspaces_purged: list[str] = field(default_factory=list)
    workspaces_skipped_active: list[str] = field(default_factory=list)
    workspace_storage_billed: list[str] = field(default_factory=list)
    orphans_reaped: list[str] = field(default_factory=list)


def _poolable_language_components(registry: Registry):
    """The pooling-eligible set (doc §4.3's own framing: "batch/workflow runs
    claim... an idle pod" — a bare language component, never a template/sidecar
    combination, matching PoolManager.is_poolable's scope). Deduped to each
    component's latest version — pooling every historical version would be pure
    waste, nothing ever requests an old one by default."""
    names = sorted(
        {
            c.metadata.name
            for c in registry.list_components()
            if c.metadata.category == "language" and c.spec.runtime.kind == "mainTool"
        }
    )
    return [registry.latest_component(name) for name in names]


async def reap_expired_sandboxes(
    *, session: AsyncSession, sandbox_service: SandboxService, now: datetime
) -> list[str]:
    stmt = select(Sandbox).where(Sandbox.state != "terminated")
    rows = (await session.execute(stmt)).scalars().all()
    reaped: list[str] = []
    for row in rows:
        idle_since = row.last_active_at or row.created_at
        idle_seconds = (now - idle_since).total_seconds()
        age_seconds = (now - row.created_at).total_seconds()
        past_idle_ttl = row.idle_ttl_seconds is not None and idle_seconds >= row.idle_ttl_seconds
        past_max_ttl = row.max_ttl_seconds is not None and age_seconds >= row.max_ttl_seconds
        if not (past_idle_ttl or past_max_ttl):
            continue
        await sandbox_service.destroy_sandbox(row.id, row.tenant_id, session, now=now)
        reaped.append(row.id)
        logger.info(
            "reconciler_ttl_reaped",
            sandbox_id=row.id,
            idle_seconds=idle_seconds,
            age_seconds=age_seconds,
            reason="idle_ttl" if past_idle_ttl else "max_ttl",
        )
    return reaped


async def replenish_pools(
    *, session: AsyncSession, pool_manager: PoolManager, registry: Registry, settings: Settings
) -> dict[str, int]:
    targets = {
        "light": settings.pool.light_pool_size,
        "standard": settings.pool.standard_pool_size,
        "heavy": settings.pool.heavy_pool_size,
    }
    added_by_component: dict[str, int] = {}
    for component in _poolable_language_components(registry):
        spec = build_ad_hoc_spec(registry, component)
        target = targets.get(spec.weight_class.value, 0)
        if target <= 0:
            continue
        added = await pool_manager.replenish_one(spec, target_count=target, session=session)
        if added:
            added_by_component[component.key] = added
    return added_by_component


async def reap_orphans(
    *, session: AsyncSession, provisioner: Provisioner, backend: str, grace_seconds: int, now: datetime
) -> list[str]:
    live_ids = set((await session.execute(select(Sandbox.id).where(Sandbox.state != "terminated"))).scalars().all())
    refs = await provisioner.list_sandbox_refs()
    reaped: list[str] = []
    for ref in refs:
        if ref.sandbox_id in live_ids:
            continue
        if (now - ref.created_at).total_seconds() < grace_seconds:
            continue  # still mid-acquire() — its row just hasn't committed yet
        handle = SandboxHandle(
            sandbox_id=ref.sandbox_id,
            backend=backend,
            native_ref=ref.native_ref,
            created_at=ref.created_at,
            sidecar_refs=ref.sidecar_refs,
        )
        await provisioner.destroy(handle)
        reaped.append(ref.sandbox_id)
        logger.warning("reconciler_orphan_reaped", sandbox_id=ref.sandbox_id, native_ref=ref.native_ref)
    return reaped


def _resolve_archiver_image(registry: Registry) -> str | None:
    try:
        return registry.resolve_component_image(registry.latest_component("base"))
    except (ComponentNotFoundError, KubeSandboxError):
        return None


async def bill_workspace_storage(
    *, session: AsyncSession, billing_service: BillingService, interval_seconds: int
) -> list[str]:
    """Prices persistent-workspace storage (doc §10.1/§13's `storage_gb_day` resource
    type) — the one usage dimension `SandboxService` has no natural call site for,
    since it accrues continuously whether or not a sandbox is currently running
    against the workspace, not per-run or per-sandbox-lifetime. Billed once per tick
    against whatever `used_mb` currently holds (refreshed by `sweep_retention()` just
    before this runs, for any workspace with no live sandbox — otherwise the last
    measurement stands, same "advisory, not real-time" honesty flag `used_mb` itself
    already carries, doc §5.4/Phase 7). `Workspace` has no `tenant_id` column of its
    own (only `user_id`); resolved here via `User.tenant_id` rather than adding one,
    since nothing else needs it.

    A workspace with `used_mb == 0` (never measured, or genuinely empty) is skipped —
    zero quantity would price to zero anyway, but skipping avoids a pointless
    `usage_records` row per idle-empty workspace per tick.
    """
    billed: list[str] = []
    gb_days = interval_seconds / 86_400
    rows = (await session.execute(select(Workspace).where(Workspace.state == "active"))).scalars().all()
    for workspace in rows:
        if workspace.used_mb <= 0:
            continue
        tenant_id = (
            await session.execute(select(User.tenant_id).where(User.id == workspace.user_id))
        ).scalar_one_or_none()
        if tenant_id is None:
            logger.warning("workspace_storage_billing_skipped_no_tenant", workspace_id=workspace.id)
            continue
        quantity = (workspace.used_mb / 1024) * gb_days
        await billing_service.record_usage(
            tenant_id,
            UsageEvent(resource_type="storage_gb_day", quantity=quantity, sandbox_id=None, run_id=None),
            session=session,
        )
        billed.append(workspace.id)
    if billed:
        await session.commit()
    return billed


async def run_tick(
    *,
    session: AsyncSession,
    provisioner: Provisioner,
    registry: Registry,
    object_storage: ObjectStorageProvider,
    settings: Settings,
) -> ReconcileTickResult:
    """One full reconciler pass — a plain function (not a method) so a unit test can
    drive it directly against a `FakeProvisioner` + in-memory session without
    standing up `ReconcilerLoop`'s own process-lifetime bits (the sleep loop, signal
    handling)."""
    now = datetime.now(UTC)
    result = ReconcileTickResult()

    pool_manager = PoolManager(provisioner) if settings.pool.enabled else None
    workspace_service = (
        WorkspaceService(
            default_quota_mb=settings.workspace.default_quota_mb,
            idle_retention_days=settings.workspace.idle_retention_days,
            archive_grace_days=settings.workspace.archive_grace_days,
            max_lifetime_days=settings.workspace.max_lifetime_days,
        )
        if settings.workspace.persistence_enabled
        else None
    )
    billing_service = BillingService(default_mode=settings.billing.default_mode) if settings.billing.enabled else None
    sandbox_service = SandboxService(
        registry, provisioner, pool_manager=pool_manager, billing_service=billing_service
    )

    result.reaped = await reap_expired_sandboxes(session=session, sandbox_service=sandbox_service, now=now)

    if pool_manager is not None:
        result.pool_replenished = await replenish_pools(
            session=session, pool_manager=pool_manager, registry=registry, settings=settings
        )

    if workspace_service is not None:
        archiver_image = _resolve_archiver_image(registry)
        if archiver_image is None:
            logger.warning("reconciler_workspace_sweep_skipped_no_base_component")
        else:
            sweep = await workspace_service.sweep_retention(
                session=session,
                provisioner=provisioner,
                object_storage=object_storage,
                archiver_image=archiver_image,
                now=now,
            )
            result.workspaces_archived = sweep.archived
            result.workspaces_purged = sweep.purged
            result.workspaces_skipped_active = sweep.skipped_active

    if billing_service is not None:
        result.workspace_storage_billed = await bill_workspace_storage(
            session=session, billing_service=billing_service, interval_seconds=settings.reconciler.interval_seconds
        )

    result.orphans_reaped = await reap_orphans(
        session=session,
        provisioner=provisioner,
        backend=settings.provisioner.backend,
        grace_seconds=settings.reconciler.orphan_grace_seconds,
        now=now,
    )

    logger.info(
        "reconciler_tick_complete",
        reaped=len(result.reaped),
        pool_replenished=sum(result.pool_replenished.values()),
        workspaces_archived=len(result.workspaces_archived),
        workspaces_purged=len(result.workspaces_purged),
        workspace_storage_billed=len(result.workspace_storage_billed),
        orphans_reaped=len(result.orphans_reaped),
    )
    return result


class ReconcilerLoop:
    """Owns the process-lifetime bits (provisioner/registry construction, the
    fixed-interval sleep loop, graceful shutdown) around `run_tick()`."""

    def __init__(
        self,
        *,
        provisioner: Provisioner,
        registry: Registry,
        object_storage: ObjectStorageProvider,
        session_factory: async_sessionmaker,
        settings: Settings,
    ) -> None:
        self._provisioner = provisioner
        self._registry = registry
        self._object_storage = object_storage
        self._session_factory = session_factory
        self._settings = settings
        self._stop = asyncio.Event()

    @classmethod
    async def create(cls, settings: Settings | None = None) -> ReconcilerLoop:
        settings = settings or get_settings()
        return cls(
            provisioner=await build_provisioner(settings),
            registry=load_registry(),
            object_storage=build_object_storage_provider(settings),
            session_factory=get_session_factory(),
            settings=settings,
        )

    async def tick(self) -> ReconcileTickResult:
        async with self._session_factory() as session:
            return await run_tick(
                session=session,
                provisioner=self._provisioner,
                registry=self._registry,
                object_storage=self._object_storage,
                settings=self._settings,
            )

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        interval = self._settings.reconciler.interval_seconds
        logger.info("reconciler_started", interval_seconds=interval)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — one bad tick must never kill the loop
                logger.exception("reconciler_tick_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass
        logger.info("reconciler_stopped")

    async def aclose(self) -> None:
        aclose = getattr(self._provisioner, "aclose", None)
        if aclose is not None:
            await aclose()


async def main() -> None:
    settings = get_settings()
    configure_logging(debug=settings.debug)
    # Same startup contract as the API (doc §9): a reconciler pointed at an
    # unimplemented cloud must die at boot, not on the first archive upload a tick
    # attempts an hour later.
    validate_cloud_providers(settings)
    # A distinct service.name so reconciler spans (workspace archive, pool
    # replenishment, TTL reaping) are separable from the API's in the trace backend —
    # they share the same Postgres and the same provisioner, so without this they'd be
    # indistinguishable.
    configure_tracing(settings, service_name=f"{settings.observability.service_name}-reconciler")
    loop = await ReconcilerLoop.create(settings)
    try:
        await loop.run_forever()
    finally:
        await loop.aclose()


if __name__ == "__main__":
    asyncio.run(main())
