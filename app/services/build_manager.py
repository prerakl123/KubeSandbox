"""BuildManager — turns a component's declared build strategy (doc §8) into a real,
pushed, runnable golden image, closing the gap `template_render.py`/`sandbox_service.py`
point at ("...only prebuilt images are runnable until BuildManager lands").

Two-phase per build, split across the request/background boundary the same way
`/v1/execute`'s `?async=true` splits create-vs-poll (doc §5.1), just mandatory here
since a real image build can take minutes: `trigger_build()` runs inside the request,
creating the `pending` Build row and returning immediately; `run_build()` does the
actual work as a FastAPI `BackgroundTask` (not a bare `asyncio.create_task`, which
risks being garbage-collected if nothing keeps a reference — `BackgroundTasks` is
Starlette's supported mechanism for exactly this), using its own session since the
request's is already closed by the time it runs.

Strategy dispatch is a fixed, built-in map keyed by `source.type` — unlike
`ComponentHook`, these four strategies are internal and not user-pluggable per
component, so there's no dotted-path plugin loading here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.build.strategies.compose import ComposeBuildStrategy
from app.build.strategies.dockerfile import DockerfileBuildStrategy
from app.build.strategies.helm import HelmChartStrategy
from app.build.strategies.pipeline import PipelineBuildStrategy
from app.cloud.registry import ImageRegistryProvider
from app.cloud.storage import ObjectStorageProvider
from app.core.errors import BuildNotFoundError, ComponentNotFoundError, EntitlementError
from app.core.logging import get_logger
from app.domain.auth import Principal
from app.domain.build import BuildContext, BuildStrategy
from app.domain.manifests import SourceType
from app.extensions.loader import Registry
from app.persistence.models import Build
from app.services.entitlement_service import EntitlementService

logger = get_logger(__name__)

_LOG_EXCERPT_LIMIT = 10_000
"""Caps Build.log_excerpt the same way Run.stdout_excerpt is bounded — a build log
isn't meant to be a full artifact store, just enough tail to diagnose a failure."""


class BuildManager:
    def __init__(
        self,
        registry: Registry,
        entitlements: EntitlementService,
        image_registry: ImageRegistryProvider,
        object_storage: ObjectStorageProvider | None,
        session_factory: async_sessionmaker[AsyncSession],
        strategies: dict[SourceType, BuildStrategy] | None = None,
    ) -> None:
        self._registry = registry
        self._entitlements = entitlements
        self._image_registry = image_registry
        self._object_storage = object_storage
        self._session_factory = session_factory
        # Injectable so unit tests can swap in fakes (no real Docker/subprocess/helm)
        # the same way FakeProvisioner is swapped into SandboxService — defaults to
        # the real, fixed, built-in map used in production.
        self._strategies: dict[SourceType, BuildStrategy] = strategies or {
            "dockerfile": DockerfileBuildStrategy(),
            "compose": ComposeBuildStrategy(),
            "pipeline": PipelineBuildStrategy(),
            "helm": HelmChartStrategy(),
        }

    def _can_build(self, component_key: str, principal: Principal) -> bool:
        if self._entitlements.is_admin(principal):
            return True
        return component_key.startswith(f"tenant/{principal.tenant_id}/")

    @staticmethod
    def _tenant_id_for(component_key: str, principal: Principal) -> str | None:
        return principal.tenant_id if component_key.startswith("tenant/") else None

    async def trigger_build(
        self, component_key: str, principal: Principal, session: AsyncSession
    ) -> tuple[Build, bool]:
        """Returns (build_row, is_new) — `is_new` is False when an in-flight build for
        this exact component was deduplicated onto instead of started; the API layer
        uses this to decide whether to schedule run_build() as a background task, so a
        duplicate request never causes the same build to run twice."""
        component = self._registry.components.get(component_key)
        if component is None:
            raise ComponentNotFoundError(component_key)
        if not self._can_build(component_key, principal):
            raise EntitlementError(
                f"not entitled to build {component_key!r} — only an admin or the "
                "owning tenant may trigger a build (doc §3.6's publish trust boundary)"
            )

        tenant_id = self._tenant_id_for(component_key, principal)
        existing = (
            await session.execute(
                select(Build).where(
                    Build.component_name == component.metadata.name,
                    Build.component_version == component.metadata.version,
                    Build.tenant_id == tenant_id,
                    Build.status.in_(["pending", "running"]),
                )
            )
        ).scalars().first()
        if existing is not None:
            return existing, False

        build_row = Build(
            component_name=component.metadata.name,
            component_version=component.metadata.version,
            tenant_id=tenant_id,
            strategy=component.spec.source.type,
            status="pending",
            requested_by=principal.user_id or principal.tenant_id,
        )
        session.add(build_row)
        await session.commit()
        await session.refresh(build_row)
        return build_row, True

    async def get_build(self, build_id: str, principal: Principal, session: AsyncSession) -> Build:
        row = await session.get(Build, build_id)
        if row is None:
            raise BuildNotFoundError(build_id)
        if not self._entitlements.is_admin(principal):
            if row.tenant_id is not None and row.tenant_id != principal.tenant_id:
                raise BuildNotFoundError(build_id)  # 404, not 403 — don't leak existence
        return row

    @staticmethod
    def _registry_key_for_build(build_row: Build) -> str:
        key = f"{build_row.component_name}@{build_row.component_version}"
        return f"tenant/{build_row.tenant_id}/{key}" if build_row.tenant_id is not None else key

    async def run_build(self, build_id: str) -> None:
        """The actual work — runs after the triggering request has already returned,
        so it opens its own session rather than reusing a request-scoped one."""
        async with self._session_factory() as session:
            build_row = await session.get(Build, build_id)
            if build_row is None:
                return

            component_key = self._registry_key_for_build(build_row)
            component = self._registry.components.get(component_key)
            component_dir = self._registry.component_dirs.get(component_key)
            if component is None or component_dir is None:
                build_row.status = "failed"
                build_row.error = f"component {component_key!r} no longer resolvable in the registry"
                build_row.finished_at = datetime.now(UTC)
                await session.commit()
                return

            strategy = self._strategies.get(component.spec.source.type)
            if strategy is None:
                build_row.status = "failed"
                build_row.error = f"no BuildStrategy registered for source.type={component.spec.source.type!r}"
                build_row.finished_at = datetime.now(UTC)
                await session.commit()
                return

            build_row.status = "running"
            build_row.started_at = datetime.now(UTC)
            await session.commit()
            logger.info("build_started", build_id=build_id, component=component_key)

            log: list[str] = []
            ctx = BuildContext(
                component_dir=component_dir,
                build_id=build_id,
                image_repo=f"kubesandbox/{component.metadata.name}",
                image_tag=component.metadata.version,
                log=log,
                object_storage=self._object_storage,
            )
            try:
                artifact = await strategy.build(component, ctx)
                if artifact.kind == "image":
                    resolved_ref = await self._image_registry.push(artifact.ref)
                    build_row.image_ref = resolved_ref
                    self._registry.built_images[component_key] = resolved_ref
                else:
                    build_row.artifact_ref = artifact.ref
                build_row.status = "succeeded"
                logger.info("build_succeeded", build_id=build_id, component=component_key)
            except Exception as exc:
                build_row.status = "failed"
                build_row.error = str(exc)
                logger.warning("build_failed", build_id=build_id, component=component_key, error=str(exc))
            finally:
                build_row.log_excerpt = "\n".join(log)[-_LOG_EXCERPT_LIMIT:]
                build_row.finished_at = datetime.now(UTC)
                await session.commit()

    async def hydrate_built_images(self, session: AsyncSession) -> None:
        """Rehydrates Registry.built_images from the latest successful Build row per
        component — otherwise a control-plane restart would forget every previously
        -built non-"image"-sourced component until it's rebuilt. Called once from
        app/main.py's lifespan, right after load_registry()."""
        rows = (
            await session.execute(
                select(Build)
                .where(Build.status == "succeeded", Build.image_ref.is_not(None))
                .order_by(Build.finished_at)
            )
        ).scalars().all()
        for row in rows:
            # Ascending order means a later (newer) successful build for the same
            # component overwrites an earlier one — the latest wins, same as any
            # other "most recent successful build" semantics in this file.
            self._registry.built_images[self._registry_key_for_build(row)] = row.image_ref
