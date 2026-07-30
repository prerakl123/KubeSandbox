"""Resolves a language/template request into a SandboxSpec + BatchCommand, drives an
ephemeral sandbox through acquire -> exec_batch -> destroy, and persists the run
(the `execute()` path, Phase 1). Also provides the non-ephemeral sandbox lifecycle
(doc §17, Phase 4 prerequisite) — `create_sandbox`/`get_sandbox`/`destroy_sandbox`/
`run_in_sandbox` — for sandboxes that outlive a single request, which interactive PTY
attach (Phase 4) and multiple batch runs against one warm sandbox both need.

Pooling (Phase 7, doc §4.3): both `execute()` and `create_sandbox()` try a warm-pool
claim before falling back to `provisioner.acquire()` — "a session may originate from a
pool claim (fast start)". Only `execute()` ever *returns* a sandbox to the pool
(`recycle()`, on a clean run of a poolable spec); `create_sandbox()`/
`destroy_sandbox()` never do — "interactive sessions are never pooled after attach...
destroyed (not recycled) on disconnect+TTL", which this generalizes to "any
non-ephemeral sandbox once it exists," not just ones that reached an attach. When
`pool_manager` is None (the constructor default, and what `pool.enabled: false`'s
config wiring passes), every acquire/release call falls straight through to the
provisioner exactly as before this phase — pooling is opt-in, never a behavior change
for a deployment that hasn't turned it on.

TTL (doc §4.1): every sandbox this service creates gets `idle_ttl_seconds`/
`max_ttl_seconds` resolved once (from its SandboxTemplate's `spec.ttl`, or the
`TTLSettings` defaults for an ad-hoc, template-less request) and persisted onto its
`Sandbox` row — the Phase 7 reconciler reads those columns to reap it later; this
service itself never enforces a TTL, it only records the inputs the reconciler needs
since it runs in a separate process/request with nothing else to read them from.

Two ways to resolve a spec either way: a single ad-hoc language component (Phase 1),
or a SandboxTemplate composed from base + multiple components (Phase 2, doc §3.4) —
see template_render.render_template for how the latter merges component specs into
one runnable SandboxSpec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    BillingAuthorizationError,
    ComponentNotFoundError,
    KubeSandboxError,
    SandboxNotFoundError,
    TemplateNotFoundError,
)
from app.core.logging import get_logger
from app.domain.execution import (
    BatchCommand,
    BatchRunResult,
    FileEntry,
    ResourceSpec,
    SandboxHandle,
    SandboxSpec,
    SandboxState,
    SandboxStatus,
    WeightClass,
)
from app.domain.manifests import Component
from app.extensions.hooks import RenderContext, load_hook
from app.extensions.loader import Registry
from app.persistence.models import Run, Sandbox, Workspace
from app.provisioners.base import Provisioner, PTYStream
from app.provisioners.resources import parse_duration_to_seconds
from app.services.billing_service import BillingService, db_sidecar_count, estimate_usage_for_spec, usage_events_for_run
from app.services.credentials import DbCredentials
from app.services.pool_manager import PoolManager
from app.services.template_render import RenderedTemplateSpec, render_template
from app.services.weight_class_scheduler import WeightClassScheduler
from app.services.workspace_service import WorkspaceService

logger = get_logger(__name__)


def build_ad_hoc_spec(registry: Registry, component: Component) -> SandboxSpec:
    """Resolves a single language/tool component into a runnable `SandboxSpec` — the
    same shape `SandboxService._build_spec` uses for an ad-hoc (template-less)
    `execute()`/`create_sandbox()` request. Standalone (not a method) so the Phase 7
    reconciler can build the exact same spec for pool replenishment (doc §4.3)
    without needing a full `SandboxService` instance."""
    runtime = component.spec.runtime
    access = component.spec.access
    return SandboxSpec(
        image=registry.resolve_component_image(component),
        command=["sleep", "infinity"],  # acquire() always launches the idle keep-alive
        workdir=access.filesystem.workdir,
        writable_paths=list(access.filesystem.writablePaths),
        read_only_root_filesystem=access.filesystem.readOnlyRootFilesystem,
        resources=ResourceSpec(cpu=runtime.resources.limits.cpu, memory=runtime.resources.limits.memory),
        weight_class=WeightClass(runtime.weightClass),
        wall_clock_seconds=access.limits.wallClockSeconds,
        max_output_bytes=access.limits.outputBytes,
        max_processes=access.limits.processes,
        labels={"io.kubesandbox.component": component.key},
    )


@dataclass
class _ResolvedSpec:
    spec: SandboxSpec
    template_ref: str | None
    component_refs: list[str]
    component: Component
    """The single component execute()/create_sandbox() should actually run code
    against — the main-tool component matching the requested language."""
    sidecar_components: list[Component] = field(default_factory=list)
    sidecar_credentials: dict[str, DbCredentials] = field(default_factory=dict)
    ttl_idle: str = ""
    ttl_max: str = ""
    """Doc §3.4 duration strings ("15m", "2h") off the resolved SandboxTemplate, if
    any — empty for an ad-hoc (template-less) request, in which case the service falls
    back to its own configured defaults (see `_resolve_ttl_seconds`)."""


class SandboxService:
    def __init__(
        self,
        registry: Registry,
        provisioner: Provisioner,
        *,
        pool_manager: PoolManager | None = None,
        default_idle_ttl_seconds: int = 900,
        default_max_ttl_seconds: int = 7_200,
        weight_class_scheduler: WeightClassScheduler | None = None,
        heavy_node_selector: dict[str, str] | None = None,
        heavy_tolerations: list[dict[str, str]] | None = None,
        workspace_service: WorkspaceService | None = None,
        billing_service: BillingService | None = None,
    ) -> None:
        self._registry = registry
        self._provisioner = provisioner
        self._pool_manager = pool_manager
        self._default_idle_ttl_seconds = default_idle_ttl_seconds
        self._default_max_ttl_seconds = default_max_ttl_seconds
        self._weight_class_scheduler = weight_class_scheduler or WeightClassScheduler()
        self._heavy_node_selector = heavy_node_selector or {}
        self._heavy_tolerations = heavy_tolerations or []
        self._workspace_service = workspace_service
        self._billing_service = billing_service

    def _resolve_component(self, language: str, version: str | None) -> Component:
        try:
            if version:
                return self._registry.get_component(language, version)
            return self._registry.latest_component(language)
        except ComponentNotFoundError as exc:
            raise ComponentNotFoundError(
                f"no such language component: {language}@{version or 'latest'}"
            ) from exc

    def _resolve_template(self, template_ref: str) -> RenderedTemplateSpec:
        try:
            template = self._registry.resolve_template_ref(template_ref)
        except TemplateNotFoundError as exc:
            raise TemplateNotFoundError(f"no such template: {template_ref}") from exc
        return render_template(self._registry, template)

    @staticmethod
    def _pick_template_component(rendered: RenderedTemplateSpec, language: str) -> Component:
        for component in rendered.main_components:
            if component.spec.provides.languageId == language or component.metadata.name == language:
                return component
        available = sorted(
            {c.spec.provides.languageId or c.metadata.name for c in rendered.main_components}
        )
        raise ComponentNotFoundError(
            f"template {rendered.template_key} has no runnable component matching "
            f"language {language!r}; available: {available}"
        )

    def _resolve_image_ref(self, component: Component) -> str:
        return self._registry.resolve_component_image(component)

    def _build_spec(self, component: Component) -> SandboxSpec:
        return build_ad_hoc_spec(self._registry, component)

    @staticmethod
    def _source_filename(component: Component) -> str:
        extensions = component.spec.provides.fileExtensions
        return f"main{extensions[0]}" if extensions else "main.txt"

    def _build_batch_command(self, component: Component, code: str, stdin: str) -> BatchCommand:
        provides = component.spec.provides
        filename = self._source_filename(component)

        if provides.batchRunner is not None:
            if not provides.commands:
                raise KubeSandboxError(f"component {component.key} declares no commands to invoke")
            command = [provides.commands[0], provides.batchRunner.entrypoint, filename]
            capture_variables = provides.batchRunner.supportsVariableDump
        elif provides.defaultRun:
            command = provides.defaultRun.format(file=filename).split()
            capture_variables = False
        else:
            raise KubeSandboxError(f"component {component.key} has neither batchRunner nor defaultRun")

        return BatchCommand(
            command=command,
            stdin=stdin,
            files={filename: code},
            timeout_seconds=component.spec.access.limits.wallClockSeconds,
            max_output_bytes=component.spec.access.limits.outputBytes,
            capture_variables=capture_variables,
        )

    def _resolve_spec(
        self, *, language: str, version: str | None, template: str | None
    ) -> _ResolvedSpec:
        """Shared by execute() and create_sandbox(): resolve a language/template
        request into everything needed to acquire() a sandbox and, if it composes any
        DB sidecars, provision them afterward. An ad-hoc single-component request
        (no template) never has sidecars — those only ever come from a
        SandboxTemplate's composed components (doc §3.4)."""
        if template:
            rendered = self._resolve_template(template)
            component = self._pick_template_component(rendered, language)
            return _ResolvedSpec(
                spec=self._apply_heavy_segregation(rendered.sandbox_spec),
                template_ref=rendered.template_key,
                component_refs=[c.key for c in rendered.main_components],
                component=component,
                sidecar_components=rendered.sidecar_components,
                sidecar_credentials=rendered.sidecar_credentials,
                ttl_idle=rendered.ttl_idle,
                ttl_max=rendered.ttl_max,
            )
        component = self._resolve_component(language, version)
        return _ResolvedSpec(
            spec=self._apply_heavy_segregation(self._build_spec(component)),
            template_ref=None,
            component_refs=[component.key],
            component=component,
        )

    def _apply_heavy_segregation(self, spec: SandboxSpec) -> SandboxSpec:
        """K8s-only node segregation for `heavy` sandboxes (doc §4.3) — a no-op for
        every other weight class, and a no-op on Docker too (DockerProvisioner never
        reads `node_selector`/`tolerations`)."""
        if spec.weight_class != WeightClass.HEAVY or not (self._heavy_node_selector or self._heavy_tolerations):
            return spec
        return spec.model_copy(
            update={"node_selector": dict(self._heavy_node_selector), "tolerations": list(self._heavy_tolerations)}
        )

    def _resolve_ttl_seconds(self, resolved: _ResolvedSpec) -> tuple[int, int]:
        idle = parse_duration_to_seconds(resolved.ttl_idle) if resolved.ttl_idle else self._default_idle_ttl_seconds
        maximum = parse_duration_to_seconds(resolved.ttl_max) if resolved.ttl_max else self._default_max_ttl_seconds
        return idle, maximum

    async def _authorize_billing(
        self, resolved: _ResolvedSpec, seconds: int, *, tenant_id: str, session: AsyncSession
    ) -> None:
        """Pre-authorization (doc §13: "before any resource is provisioned") — a no-op
        whenever billing isn't wired up (`billing_service is None`, what `billing.enabled:
        false` — the default — passes), identical to every other opt-in feature in this
        service. `seconds` is a ceiling, not a promise of the run's actual duration:
        `execute()` passes its own wall-clock cap; `create_sandbox()` passes
        `max_ttl_seconds`, since a warm sandbox's real lifetime isn't known upfront but
        the reconciler guarantees it never outlives that TTL."""
        if self._billing_service is None:
            return
        estimate = estimate_usage_for_spec(
            resolved.spec.resources, seconds, db_sidecar_count=db_sidecar_count(resolved.sidecar_components)
        )
        auth = await self._billing_service.authorize(tenant_id, estimate, session=session)
        if not auth.authorized:
            raise BillingAuthorizationError(auth.reason or "billing authorization denied")

    async def _acquire_for_execute(self, spec: SandboxSpec, session: AsyncSession) -> SandboxHandle:
        """Warm-pool claim first (doc §4.3), falling back to a fresh
        `provisioner.acquire()` on a pool miss or when pooling isn't wired up
        (`pool_manager is None`) — identical to pre-Phase-7 behavior either way."""
        if self._pool_manager is not None:
            claimed = await self._pool_manager.try_claim(spec, session=session)
            if claimed is not None:
                return claimed
        return await self._provisioner.acquire(spec)

    @staticmethod
    def _is_clean_run(result: BatchRunResult) -> bool:
        """Doc §4.3: "after a batch run completes cleanly" — about the sandbox's own
        health, not the user's program's exit code. A non-zero exit is still a clean
        run of a perfectly healthy sandbox; a timeout or truncated output means the
        sandbox hit a resource limit and shouldn't be trusted to go back in the pool."""
        return not result.timed_out and not result.truncated

    async def _release_or_destroy(
        self, handle: SandboxHandle, spec: SandboxSpec, result: BatchRunResult | None, session: AsyncSession
    ) -> None:
        if (
            result is not None
            and self._is_clean_run(result)
            and self._pool_manager is not None
            and PoolManager.is_poolable(spec)
        ):
            await self._pool_manager.release(handle, spec, session=session)
            return
        await self._provisioner.destroy(handle)

    async def _provision_sidecars(
        self,
        handle: SandboxHandle,
        sidecar_components: list[Component],
        sidecar_credentials: dict[str, DbCredentials],
    ) -> None:
        """Runs each composed DB sidecar's on_provision hook (doc §3.5, Phase 5) right
        after acquire() succeeds — e.g. creating the scoped Postgres role main's
        DATABASE_URL env var already promised. Only sidecars that declare both a hook
        module AND access.database get here; template_render only populates
        sidecar_credentials for those, so the two collections stay in lockstep."""
        for component in sidecar_components:
            if component.spec.hooks is None:
                continue
            credentials = sidecar_credentials.get(component.metadata.name)
            if credentials is None:
                continue
            hook = load_hook(component.spec.hooks.module)
            ctx = RenderContext(component=component, credentials=credentials, provisioner=self._provisioner)
            await hook.on_provision(handle, ctx)

    async def _teardown_sidecars(self, handle: SandboxHandle, sidecar_components: list[Component]) -> None:
        """Runs each composed DB sidecar's on_teardown hook before the sandbox itself
        is destroyed. Best-effort: a hook failure must never block the sandbox's own
        destruction (every current hook's on_teardown is a documented no-op anyway —
        see components/databases/*/hooks.py — so this is defensive, not load-bearing)."""
        for component in sidecar_components:
            if component.spec.hooks is None:
                continue
            try:
                hook = load_hook(component.spec.hooks.module)
                await hook.on_teardown(handle)
            except Exception as exc:  # noqa: BLE001 — teardown must never block destroy()
                logger.warning(
                    "sidecar_teardown_hook_failed",
                    sandbox_id=handle.sandbox_id,
                    component=component.key,
                    error=str(exc),
                )

    @staticmethod
    def _handle_from_row(row: Sandbox) -> SandboxHandle:
        return SandboxHandle(
            sandbox_id=row.id,
            backend=row.backend,
            native_ref=row.native_ref,
            created_at=row.created_at,
            sidecar_refs=row.sidecar_refs,
            persistent=row.persistent,
        )

    def _sidecar_components_for_row(self, row: Sandbox) -> list[Component]:
        """Re-derives a persisted sandbox's sidecar Components from its template_ref
        (destroy_sandbox() runs in a separate request from create_sandbox()/execute(),
        so nothing in-memory survives to reuse — the row's template_ref is the only
        thing that does). An ad-hoc, non-template sandbox never has sidecars."""
        if not row.template_ref:
            return []
        rendered = self._resolve_template(row.template_ref)
        return rendered.sidecar_components

    def _spec_resources_for_row(self, row: Sandbox) -> ResourceSpec:
        """Re-derives a persisted sandbox's resource limits, the same way
        `_sidecar_components_for_row` re-derives its sidecar Components — `Sandbox`
        doesn't store its own `SandboxSpec.resources` directly, only enough (
        `template_ref`/`component_refs`) to reconstruct it. Needed by
        `destroy_sandbox()` to price a non-ephemeral sandbox's actual lifetime usage
        (doc §13) without re-deriving the whole spec (image/command/etc. it doesn't
        need for that)."""
        if row.template_ref:
            return self._resolve_template(row.template_ref).sandbox_spec.resources
        if not row.component_refs:
            raise KubeSandboxError(f"sandbox {row.id} has no known runnable component")
        component = self._registry.resolve_component_ref(row.component_refs[0])
        return self._build_spec(component).resources

    def _resolve_component_for_run(self, row: Sandbox, language: str | None) -> Component:
        """Which component a POST .../runs call should execute against — the same
        component(s) the sandbox was created with, not a fresh registry lookup by
        version-or-latest (that could silently drift from what's actually running)."""
        if row.template_ref:
            rendered = self._resolve_template(row.template_ref)
            if language:
                return self._pick_template_component(rendered, language)
            if len(rendered.main_components) == 1:
                return rendered.main_components[0]
            raise KubeSandboxError(
                f"sandbox {row.id} was created from template {row.template_ref!r}, which "
                f"has {len(rendered.main_components)} runnable components; specify "
                "'language' to pick one"
            )
        if not row.component_refs:
            raise KubeSandboxError(f"sandbox {row.id} has no known runnable component")
        return self._registry.resolve_component_ref(row.component_refs[0])

    async def execute(
        self,
        *,
        language: str,
        code: str,
        version: str | None = None,
        stdin: str = "",
        template: str | None = None,
        tenant_id: str,
        user_id: str | None,
        session: AsyncSession,
    ) -> BatchRunResult:
        resolved = self._resolve_spec(language=language, version=version, template=template)
        batch_command = self._build_batch_command(resolved.component, code, stdin)
        idle_ttl_seconds, max_ttl_seconds = self._resolve_ttl_seconds(resolved)
        await self._authorize_billing(
            resolved, resolved.spec.wall_clock_seconds, tenant_id=tenant_id, session=session
        )

        # Held for the whole acquire->run->release/destroy lifetime, not just the
        # acquire — doc §4.3's "must not starve light ones" means capping concurrently
        # *running* heavy sandboxes, not just concurrent acquisitions (doc §7's local
        # stand-in for aks-prod's real node-pool segregation; see
        # WeightClassScheduler's own docstring for why this is Docker/local-only and
        # scoped to execute()'s fully self-contained lifetime).
        async with self._weight_class_scheduler.slot(resolved.spec.weight_class):
            handle = await self._acquire_for_execute(resolved.spec, session)

            sandbox_row = Sandbox(
                id=handle.sandbox_id,
                tenant_id=tenant_id,
                user_id=user_id,
                template_ref=resolved.template_ref,
                component_refs=resolved.component_refs,
                backend=handle.backend,
                native_ref=handle.native_ref,
                sidecar_refs=handle.sidecar_refs,
                state="active",
                weight_class=resolved.spec.weight_class.value,
                persistent=False,
                idle_ttl_seconds=idle_ttl_seconds,
                max_ttl_seconds=max_ttl_seconds,
            )
            session.add(sandbox_row)
            await session.flush()

            result: BatchRunResult | None = None
            try:
                await self._provision_sidecars(handle, resolved.sidecar_components, resolved.sidecar_credentials)
                result = await self._provisioner.exec_batch(handle, batch_command)
            finally:
                # Graceful eradication (doc §4.1): always tear down or release the
                # ephemeral sandbox, whether sidecar provisioning or the run itself
                # succeeded, failed, or timed out — a clean, poolable, pooling-enabled run
                # goes back to the warm pool (doc §4.3); everything else is destroyed.
                await self._teardown_sidecars(handle, resolved.sidecar_components)
                await self._release_or_destroy(handle, resolved.spec, result, session)

        run_row = Run(
            sandbox_id=handle.sandbox_id,
            tenant_id=tenant_id,
            command=batch_command.command,
            exit_code=result.exit_code,
            stdout_excerpt=result.stdout[:10_000],
            stderr_excerpt=result.stderr[:10_000],
            variables=result.variables,
            truncated=result.truncated,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
        )
        session.add(run_row)
        sandbox_row.state = "terminated"

        if self._billing_service is not None:
            await session.flush()  # populates run_row.id for the usage records below
            for event in usage_events_for_run(
                resolved.spec.resources,
                result.duration_ms,
                sandbox_id=handle.sandbox_id,
                run_id=run_row.id,
                db_sidecar_count=db_sidecar_count(resolved.sidecar_components),
            ):
                await self._billing_service.record_usage(tenant_id, event, session=session)

        await session.commit()

        return result

    # -- non-ephemeral sandbox lifecycle (doc §17, Phase 4 prerequisite) ----------------
    # `attach()` (Phase 4) and running more than one batch command against a warm
    # sandbox both need a sandbox that outlives a single request — unlike execute()
    # above, none of these destroy the sandbox themselves.

    async def create_sandbox(
        self,
        *,
        language: str,
        version: str | None = None,
        template: str | None = None,
        persistent: bool = False,
        tenant_id: str,
        user_id: str | None,
        session: AsyncSession,
    ) -> Sandbox:
        resolved = self._resolve_spec(language=language, version=version, template=template)
        idle_ttl_seconds, max_ttl_seconds = self._resolve_ttl_seconds(resolved)
        await self._authorize_billing(resolved, max_ttl_seconds, tenant_id=tenant_id, session=session)

        workspace_id: str | None = None
        if persistent:
            if self._workspace_service is None:
                raise KubeSandboxError("persistent workspaces are not enabled in this environment")
            if user_id is None:
                raise KubeSandboxError("a persistent sandbox requires an authenticated user")
            workspace = await self._workspace_service.get_or_create(user_id, session=session)
            if workspace.state != "active":
                # An archived/deleted workspace's durable volume/PVC no longer exists
                # (retention's archive step removes it right after a successful cold-
                # storage upload, doc §10.2) — silently mounting workspace_id here
                # would just create a fresh, EMPTY volume under the same name, not
                # restore anything. Restoring is a deliberate, separate, potentially
                # slow operation (WorkspaceService.restore()) — never an implicit side
                # effect of "just create the sandbox," so this fails loudly instead.
                raise KubeSandboxError(
                    f"workspace {workspace.id} is {workspace.state!r}, not active — call "
                    "WorkspaceService.restore() first if you want its data back"
                )
            self._workspace_service.check_quota(workspace)
            self._workspace_service.touch(workspace)
            workspace_id = workspace.id
            resolved.spec = resolved.spec.model_copy(
                update={"workspace_id": workspace.id, "workspace_size_mb": workspace.quota_mb}
            )

        handle = await self._acquire_for_execute(resolved.spec, session)

        try:
            await self._provision_sidecars(handle, resolved.sidecar_components, resolved.sidecar_credentials)
        except Exception:
            # Nothing's been persisted yet at this point — the caller has no id to
            # destroy this by later, so a provisioning failure must clean up here or
            # the sandbox (and its sidecar) leaks with no way to reach it again. Always
            # destroy, never release to the pool: a sandbox whose own sidecar failed
            # to provision is not a clean, poolable resource (doc §4.3).
            await self._provisioner.destroy(handle)
            raise

        sandbox_row = Sandbox(
            id=handle.sandbox_id,
            tenant_id=tenant_id,
            user_id=user_id,
            template_ref=resolved.template_ref,
            component_refs=resolved.component_refs,
            backend=handle.backend,
            native_ref=handle.native_ref,
            sidecar_refs=handle.sidecar_refs,
            state="active",
            weight_class=resolved.spec.weight_class.value,
            persistent=persistent,
            workspace_id=workspace_id,
            idle_ttl_seconds=idle_ttl_seconds,
            max_ttl_seconds=max_ttl_seconds,
        )
        session.add(sandbox_row)
        await session.commit()
        await session.refresh(sandbox_row)
        return sandbox_row

    async def get_sandbox(self, sandbox_id: str, tenant_id: str, session: AsyncSession) -> Sandbox:
        """Ownership-checked row lookup — a tenant mismatch is reported identically to
        'doesn't exist' (SandboxNotFoundError -> 404) so a caller can't probe for other
        tenants' sandbox ids by distinguishing 403 from 404."""
        row = await session.get(Sandbox, sandbox_id)
        if row is None or row.tenant_id != tenant_id:
            raise SandboxNotFoundError(sandbox_id)
        return row

    async def get_sandbox_status(
        self, sandbox_id: str, tenant_id: str, session: AsyncSession
    ) -> tuple[Sandbox, SandboxStatus]:
        """Live status (doc §17 `GET /v1/sandboxes/{id}`) — asks the provisioner, not
        just the last-known DB row, and opportunistically self-heals the row if the
        provisioner reports the sandbox is actually gone (e.g. reaped out-of-band)."""
        row = await self.get_sandbox(sandbox_id, tenant_id, session)
        if row.state == "terminated":
            return row, SandboxStatus(sandbox_id=row.id, state=SandboxState.TERMINATED)

        status = await self._provisioner.status(self._handle_from_row(row))
        if status.state == SandboxState.TERMINATED and row.state != "terminated":
            row.state = "terminated"
            row.terminated_at = datetime.now(UTC)
            await session.commit()
        return row, status

    async def destroy_sandbox(
        self, sandbox_id: str, tenant_id: str, session: AsyncSession, *, now: datetime | None = None
    ) -> None:
        """`now` is settable so a caller reaping many sandboxes in one pass (the
        reconciler's `reap_expired_sandboxes()`) can bill every one of them against the
        same tick timestamp instead of each hitting a slightly different real
        `datetime.now(UTC)` — matters for tests, harmless drift otherwise."""
        row = await self.get_sandbox(sandbox_id, tenant_id, session)
        if row.state == "terminated":
            return  # idempotent, mirrors Provisioner.destroy()'s own idempotency
        handle = self._handle_from_row(row)
        sidecar_components = self._sidecar_components_for_row(row)
        await self._teardown_sidecars(handle, sidecar_components)
        await self._provisioner.destroy(handle)
        now = now or datetime.now(UTC)

        if self._billing_service is not None:
            # Bills this non-ephemeral sandbox's whole real lifetime (created_at ->
            # now), closing the gap execute()'s own per-run billing doesn't cover —
            # a warm/interactive sandbox was only ever *authorized* at create time
            # (against a max_ttl_seconds ceiling), never actually billed until now.
            # Also covers a TTL reap for free: the reconciler's reap_expired_sandboxes()
            # already calls destroy_sandbox() directly, so no separate wiring is needed
            # there (see usage_events_for_run's own docstring).
            duration_ms = max(0, int((now - row.created_at).total_seconds() * 1000))
            for event in usage_events_for_run(
                self._spec_resources_for_row(row),
                duration_ms,
                sandbox_id=row.id,
                run_id=None,
                db_sidecar_count=db_sidecar_count(sidecar_components),
            ):
                await self._billing_service.record_usage(tenant_id, event, session=session)

        row.state = "terminated"
        row.terminated_at = now
        await session.commit()

    async def open_pty(self, sandbox_id: str, tenant_id: str, session: AsyncSession) -> PTYStream:
        """Interactive attach (doc §5.2, Phase 4) — the WS gateway's only touchpoint
        into Provisioner-land, keeping SandboxService the sole seam that talks to a
        Provisioner directly (doc §4.2's whole point)."""
        row = await self.get_sandbox(sandbox_id, tenant_id, session)
        if row.state == "terminated":
            raise SandboxNotFoundError(sandbox_id)
        await self._touch_workspace(row, session)
        return await self._provisioner.attach(self._handle_from_row(row))

    async def _touch_workspace(self, row: Sandbox, session: AsyncSession) -> None:
        """Doc §10.2 retention is keyed off "last session activity" — a persistent
        sandbox's own workspace must reflect real use (attach, or a batch run against
        it), not just its creation time, or the reconciler's idle-retention sweep
        could archive an actively-used workspace out from under a long-lived session."""
        if row.workspace_id is None:
            return
        workspace = await session.get(Workspace, row.workspace_id)
        if workspace is not None:
            workspace.last_access_at = datetime.now(UTC)
            await session.commit()

    async def _live_handle(self, sandbox_id: str, tenant_id: str, session: AsyncSession) -> SandboxHandle:
        row = await self.get_sandbox(sandbox_id, tenant_id, session)
        if row.state == "terminated":
            raise SandboxNotFoundError(sandbox_id)
        return self._handle_from_row(row)

    async def get_file(self, sandbox_id: str, tenant_id: str, path: str, *, session: AsyncSession) -> bytes:
        """Download (doc §5.4) — `path` is an absolute in-sandbox path, already
        resolved/bounded to the workspace by the API layer."""
        handle = await self._live_handle(sandbox_id, tenant_id, session)
        return await self._provisioner.get_file(handle, path)

    async def put_file(
        self, sandbox_id: str, tenant_id: str, relative_path: str, content: str, *, session: AsyncSession
    ) -> None:
        """Upload (doc §5.4) — reuses put_files()'s existing workspace-relative-path
        contract (the same one exec_batch's `files` argument uses)."""
        handle = await self._live_handle(sandbox_id, tenant_id, session)
        await self._provisioner.put_files(handle, {relative_path: content})

    async def list_tree(
        self, sandbox_id: str, tenant_id: str, path: str, *, session: AsyncSession
    ) -> list[FileEntry]:
        handle = await self._live_handle(sandbox_id, tenant_id, session)
        return await self._provisioner.list_tree(handle, path)

    async def run_in_sandbox(
        self,
        sandbox_id: str,
        *,
        code: str,
        stdin: str = "",
        language: str | None = None,
        tenant_id: str,
        session: AsyncSession,
    ) -> BatchRunResult:
        """POST /v1/sandboxes/{id}/runs (doc §5.1's last bullet) — same batch contract
        as execute(), against an existing warm sandbox instead of a fresh ephemeral
        one. Never destroys the sandbox, win or lose."""
        row = await self.get_sandbox(sandbox_id, tenant_id, session)
        if row.state == "terminated":
            raise SandboxNotFoundError(sandbox_id)

        component = self._resolve_component_for_run(row, language)
        batch_command = self._build_batch_command(component, code, stdin)
        handle = self._handle_from_row(row)

        result = await self._provisioner.exec_batch(handle, batch_command)

        row.last_active_at = datetime.now(UTC)
        await self._touch_workspace(row, session)
        session.add(
            Run(
                sandbox_id=row.id,
                tenant_id=tenant_id,
                command=batch_command.command,
                exit_code=result.exit_code,
                stdout_excerpt=result.stdout[:10_000],
                stderr_excerpt=result.stderr[:10_000],
                variables=result.variables,
                truncated=result.truncated,
                timed_out=result.timed_out,
                duration_ms=result.duration_ms,
            )
        )
        await session.commit()
        return result
