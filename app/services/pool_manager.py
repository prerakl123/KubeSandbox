"""Warm-pool claim/release/replenish (doc §4.3, Phase 7).

Scope decision: only ad-hoc, sidecar-less, non-persistent sandboxes are ever pooled —
the exact shape `SandboxService.execute()`'s ephemeral batch-run path produces for a
single language component. Doc §4.3 itself frames pooling around "batch/workflow
runs" claiming "an idle pod... instead of creating one from scratch"; a
SandboxTemplate composing DB sidecars needs per-tenant credentials generated and
`on_provision` hooks run before it's usable (Phase 5), and a persistent sandbox is
tied to one specific workspace's durable volume/PVC (Phase 7's other half) — neither
kind of sandbox is generic enough to hand to a *different* tenant's next request, so
neither is ever pooled. "Interactive sessions are never pooled after attach" (doc
§4.3) is the same principle already stated for Phase 4's world; this extends it to
"never pooled in the first place" for anything that isn't a bare language component.

Claim/release ledger lives in Postgres (`pool_members`), not Redis, despite doc
§10.1's Redis line item for "pool claim locks" — `SELECT ... FOR UPDATE SKIP LOCKED`
gives an atomic, exactly-once claim directly against the same table that's the actual
source of truth for "which sandbox is this", with no separate cache to keep in sync
or leak on a crash between "claimed in Redis" and "reflected in Postgres". This still
satisfies doc's "any replica can serve any session" requirement (§2) — the claim is a
single atomically-committed row mutation, visible to every replica through the same
database, not replica-local state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ProvisionerError
from app.core.logging import get_logger
from app.domain.execution import SandboxHandle, SandboxSpec
from app.persistence.models import PoolMember, PoolState
from app.provisioners.base import Provisioner

logger = get_logger(__name__)


class PoolManager:
    def __init__(self, provisioner: Provisioner) -> None:
        self._provisioner = provisioner

    @staticmethod
    def is_poolable(spec: SandboxSpec) -> bool:
        return not spec.sidecars and spec.workspace_id is None

    async def try_claim(self, spec: SandboxSpec, *, session: AsyncSession) -> SandboxHandle | None:
        """Atomically claim one idle pool member matching `(spec.image,
        spec.weight_class)`, or return None on a pool miss (the caller falls back to
        `provisioner.acquire()`, exactly as if pooling didn't exist)."""
        if not self.is_poolable(spec):
            return None

        weight_class = spec.weight_class.value
        stmt = (
            select(PoolMember)
            .where(PoolMember.image_ref == spec.image, PoolMember.weight_class == weight_class)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        member = (await session.execute(stmt)).scalar_one_or_none()
        if member is None:
            return None

        handle = SandboxHandle(
            sandbox_id=str(uuid.uuid4()),
            backend=member.backend,
            native_ref=member.native_ref,
            created_at=datetime.now(UTC),
        )
        await session.delete(member)
        await self._sync_pool_state(session, spec.image, weight_class)
        logger.info("pool_claim_hit", image=spec.image, weight_class=weight_class, native_ref=member.native_ref)
        return handle

    async def release(self, handle: SandboxHandle, spec: SandboxSpec, *, session: AsyncSession) -> None:
        """Return a sandbox to its pool after a clean run (doc §4.3): wipe its
        workspace via `recycle()` and re-list it as claimable. A `recycle()` failure
        (the container/pod itself is no longer healthy) falls back to `destroy()`
        instead of leaking a broken entry into the pool — "if the run errored... the
        pod is destroyed instead, to avoid leaking dirty state" applies just as much
        to a recycle-time failure as to the run itself failing."""
        if not self.is_poolable(spec):
            # Defense in depth: SandboxService already gates this call on
            # is_poolable(), but a non-poolable spec (sidecars, a persistent
            # workspace) must never end up claimable by an unrelated tenant even if
            # some future caller forgets to check first.
            await self._provisioner.destroy(handle)
            return
        try:
            await self._provisioner.recycle(handle)
        except ProvisionerError as exc:
            logger.warning(
                "pool_release_recycle_failed_destroying_instead",
                sandbox_id=handle.sandbox_id,
                native_ref=handle.native_ref,
                error=str(exc),
            )
            await self._provisioner.destroy(handle)
            return

        weight_class = spec.weight_class.value
        session.add(
            PoolMember(
                image_ref=spec.image,
                weight_class=weight_class,
                backend=handle.backend,
                native_ref=handle.native_ref,
            )
        )
        await self._sync_pool_state(session, spec.image, weight_class)
        logger.info("pool_release", image=spec.image, weight_class=weight_class, native_ref=handle.native_ref)

    async def replenish_one(
        self, spec: SandboxSpec, *, target_count: int, session: AsyncSession
    ) -> int:
        """Tops up the idle pool for `(spec.image, spec.weight_class)` up to
        `target_count`, acquiring fresh sandboxes from the provisioner as needed.
        Returns how many were actually added (fewer than requested if an acquire()
        call fails partway — logged, not raised, since a partial top-up is still
        useful and the reconciler tick loop should keep running for other keys)."""
        weight_class = spec.weight_class.value
        current = await self._idle_count(session, spec.image, weight_class)
        added = 0
        for _ in range(max(0, target_count - current)):
            try:
                handle = await self._provisioner.acquire(spec)
            except ProvisionerError as exc:
                logger.warning(
                    "pool_replenish_acquire_failed", image=spec.image, weight_class=weight_class, error=str(exc)
                )
                break
            session.add(
                PoolMember(
                    image_ref=spec.image,
                    weight_class=weight_class,
                    backend=handle.backend,
                    native_ref=handle.native_ref,
                )
            )
            added += 1
        if added:
            await self._sync_pool_state(session, spec.image, weight_class)
            logger.info("pool_replenished", image=spec.image, weight_class=weight_class, added=added)
        return added

    async def _idle_count(self, session: AsyncSession, image_ref: str, weight_class: str) -> int:
        stmt = select(PoolMember).where(PoolMember.image_ref == image_ref, PoolMember.weight_class == weight_class)
        return len((await session.execute(stmt)).scalars().all())

    async def _sync_pool_state(self, session: AsyncSession, image_ref: str, weight_class: str) -> None:
        """Recomputes the observability counter (doc §14 `pool_hit_rate`'s
        denominator) from `pool_members` directly rather than incrementing/
        decrementing a cached count — always consistent, never drifts."""
        count = await self._idle_count(session, image_ref, weight_class)
        stmt = select(PoolState).where(PoolState.image_digest == image_ref, PoolState.weight_class == weight_class)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            session.add(PoolState(image_digest=image_ref, weight_class=weight_class, idle_count=count))
        else:
            row.idle_count = count
            row.updated_at = datetime.now(UTC)
