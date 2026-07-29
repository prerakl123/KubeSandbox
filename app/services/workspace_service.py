"""Persistent workspace bookkeeping (doc §10.2, Phase 7) — one `Workspace` row per
user, created lazily on first persistent sandbox request. Quota/retention numbers
live here as plain config-driven values (doc: "generous starting margins... an admin
can tighten/loosen them without a code change").

`sweep_retention()` is the archive/purge state machine (`active -> archived ->
deleted`), driven by the reconciler (a separate process, doc §4.1) rather than
anything request-scoped — it needs a Provisioner (to actually move data off a durable
volume/PVC) and an ObjectStorageProvider (to hold the cold copy), both call-time
parameters rather than constructor state, since the lightweight per-request
WorkspaceService built in `app/api/deps.py` never needs either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cloud.storage import ObjectStorageProvider
from app.core.errors import KubeSandboxError, ProvisionerError, QuotaExceededError
from app.core.logging import get_logger
from app.persistence.models import Sandbox, Workspace
from app.provisioners.base import Provisioner

logger = get_logger(__name__)


def _archive_key(workspace_id: str) -> str:
    return f"workspaces/{workspace_id}/archive.tar.gz"


@dataclass
class RetentionSweepResult:
    archived: list[str] = field(default_factory=list)
    purged: list[str] = field(default_factory=list)
    skipped_active: list[str] = field(default_factory=list)
    """Workspaces that qualified for archival by age/idleness but still have a live
    (non-terminated) sandbox referencing them — archiving out from under a running
    session would corrupt it, so these wait for their next sweep instead."""


class WorkspaceService:
    def __init__(
        self,
        *,
        default_quota_mb: int,
        idle_retention_days: int = 30,
        archive_grace_days: int = 60,
        max_lifetime_days: int = 365,
    ) -> None:
        self._default_quota_mb = default_quota_mb
        self._idle_retention_days = idle_retention_days
        self._archive_grace_days = archive_grace_days
        self._max_lifetime_days = max_lifetime_days

    async def get_or_create(self, user_id: str, *, session: AsyncSession) -> Workspace:
        row = (
            await session.execute(select(Workspace).where(Workspace.user_id == user_id))
        ).scalar_one_or_none()
        if row is not None:
            return row
        row = Workspace(user_id=user_id, quota_mb=self._default_quota_mb)
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    def check_quota(workspace: Workspace) -> None:
        """Doc §5.4: workspace size is "bounded by workspace size" — soft, not a real
        filesystem quota (xfs project quotas / cgroups could enforce one; that's a
        genuine gap, flagged rather than silently pretended, same as Phase 5's
        unenforced `maxDbSizeMB`). This checks the *last measured* usage recorded on
        the row; it's the reconciler's retention sweep that keeps `used_mb` fresh."""
        if workspace.used_mb > workspace.quota_mb:
            raise QuotaExceededError(
                f"workspace {workspace.id} is over quota: {workspace.used_mb}MB used of {workspace.quota_mb}MB"
            )

    @staticmethod
    def touch(workspace: Workspace) -> None:
        workspace.last_access_at = datetime.now(UTC)

    async def _has_live_sandbox(self, workspace_id: str, session: AsyncSession) -> bool:
        stmt = select(Sandbox).where(Sandbox.workspace_id == workspace_id, Sandbox.state != "terminated")
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def sweep_retention(
        self,
        *,
        session: AsyncSession,
        provisioner: Provisioner,
        object_storage: ObjectStorageProvider,
        archiver_image: str,
        now: datetime | None = None,
    ) -> RetentionSweepResult:
        """Doc §10.2's retention table, applied per workspace:

        - `active`, idle >= idle_retention_days (default 30) OR age >=
          max_lifetime_days (default 365, "requires explicit renewal... else follows
          the same archive->delete path") -> archive: tar the durable volume/PVC,
          upload it, delete the volume/PVC, flip to `archived`.
        - `archived`, idle >= idle_retention_days + archive_grace_days (default 90
          total) -> purge: delete the cold copy, flip to `deleted`.

        Both thresholds are measured from `last_access_at` — doc's own wording ties
        the archive-grace clock to "last session activity", not to whenever archival
        happened to run.

        Also refreshes `used_mb` (via `Provisioner.measure_workspace_usage()`) for
        every `active` workspace with no currently-live sandbox, before evaluating
        retention — otherwise `WorkspaceService.check_quota()` would enforce against
        a value nothing ever updates. Skipped for a workspace with a live sandbox: a
        Kubernetes PVC is `ReadWriteOnce` (at most one node's pods can mount it
        concurrently on most CSI drivers), so a second, measurement-only pod could
        fail to schedule if the real sandbox pod landed on a different node — not
        worth the risk for a number that's advisory anyway (doc §5.4: "bounded by
        workspace size" is soft, not a hard filesystem quota). Runs on every tick
        (`reconciler.interval_seconds`, default 30s) — a real per-workspace cost
        worth tuning down (e.g. skip if measured recently) if this ever runs against
        many thousands of workspaces; not attempted here since nothing currently
        needs it at that scale.
        """
        now = now or datetime.now(UTC)
        result = RetentionSweepResult()
        rows = (await session.execute(select(Workspace).where(Workspace.state != "deleted"))).scalars().all()

        for workspace in rows:
            if workspace.state == "active" and not await self._has_live_sandbox(workspace.id, session):
                try:
                    workspace.used_mb = await provisioner.measure_workspace_usage(
                        workspace.id, archiver_image=archiver_image
                    )
                except ProvisionerError as exc:
                    logger.warning("workspace_usage_measurement_failed", workspace_id=workspace.id, error=str(exc))

            idle_days = (now - workspace.last_access_at).days
            age_days = (now - workspace.created_at).days

            if workspace.state == "active":
                if idle_days < self._idle_retention_days and age_days < self._max_lifetime_days:
                    continue
                if await self._has_live_sandbox(workspace.id, session):
                    result.skipped_active.append(workspace.id)
                    continue
                data = await provisioner.archive_workspace(workspace.id, archiver_image=archiver_image)
                await object_storage.put(_archive_key(workspace.id), data)
                await provisioner.delete_workspace_volume(workspace.id)
                workspace.state = "archived"
                result.archived.append(workspace.id)
                logger.info("workspace_archived", workspace_id=workspace.id, idle_days=idle_days, age_days=age_days)
            elif workspace.state == "archived":
                if idle_days < self._idle_retention_days + self._archive_grace_days:
                    continue
                await object_storage.delete(_archive_key(workspace.id))
                workspace.state = "deleted"
                result.purged.append(workspace.id)
                logger.info("workspace_purged", workspace_id=workspace.id, idle_days=idle_days)

        await session.commit()
        return result

    async def restore(
        self,
        workspace: Workspace,
        *,
        provisioner: Provisioner,
        object_storage: ObjectStorageProvider,
        archiver_image: str,
    ) -> None:
        """Explicit opt-in restore for an `archived` workspace (doc §10.2): fetches its
        cold-storage tar and re-populates a fresh volume/PVC via
        `Provisioner.restore_workspace()`, then flips it back to `active`.

        Deliberately NOT invoked automatically by
        `SandboxService.create_sandbox(persistent=True)` — that raises instead on an
        archived workspace, rather than silently deciding a slow, explicit
        cold-storage fetch is what the caller wanted as a side effect of "just create
        the sandbox." A caller that DOES want the data back calls this first."""
        if workspace.state != "archived":
            raise KubeSandboxError(
                f"workspace {workspace.id} is {workspace.state!r}, not archived — nothing to restore"
            )
        data = await object_storage.get(_archive_key(workspace.id))
        await provisioner.restore_workspace(workspace.id, data, archiver_image=archiver_image)
        workspace.state = "active"
        workspace.last_access_at = datetime.now(UTC)
        logger.info("workspace_restored", workspace_id=workspace.id)
