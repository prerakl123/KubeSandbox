"""Resolves a language/template request into a SandboxSpec + BatchCommand, drives an
ephemeral sandbox through acquire -> exec_batch -> destroy, and persists the run
(the `execute()` path, Phase 1). Also provides the non-ephemeral sandbox lifecycle
(doc §17, Phase 4 prerequisite) — `create_sandbox`/`get_sandbox`/`destroy_sandbox`/
`run_in_sandbox` — for sandboxes that outlive a single request, which interactive PTY
attach (Phase 4) and multiple batch runs against one warm sandbox both need.

No pooling/recycling yet (Phase 7) — `execute()`'s ephemeral sandboxes are always
destroyed after one run; sandboxes created via `create_sandbox()` live until
`destroy_sandbox()` is called (or the Phase 7 reconciler reaps them by TTL, not yet
wired). Two ways to resolve a spec either way: a single ad-hoc language component
(Phase 1), or a SandboxTemplate composed from base + multiple components (Phase 2, doc
§3.4) — see template_render.render_template for how the latter merges component specs
into one runnable SandboxSpec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
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
from app.persistence.models import Run, Sandbox
from app.provisioners.base import Provisioner, PTYStream
from app.services.credentials import DbCredentials
from app.services.template_render import RenderedTemplateSpec, render_template

logger = get_logger(__name__)


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


class SandboxService:
    def __init__(self, registry: Registry, provisioner: Provisioner) -> None:
        self._registry = registry
        self._provisioner = provisioner

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

    @staticmethod
    def _resolve_image_ref(component: Component) -> str:
        source = component.spec.source
        if source.type != "image" or source.image is None:
            raise KubeSandboxError(
                f"component {component.key} uses source.type={source.type!r}; only "
                "prebuilt images are runnable until BuildManager lands (roadmap Phase 6)"
            )
        return f"{source.image.repository}:{source.image.tag}"

    def _build_spec(self, component: Component) -> SandboxSpec:
        runtime = component.spec.runtime
        access = component.spec.access
        return SandboxSpec(
            image=self._resolve_image_ref(component),
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
                spec=rendered.sandbox_spec,
                template_ref=rendered.template_key,
                component_refs=[c.key for c in rendered.main_components],
                component=component,
                sidecar_components=rendered.sidecar_components,
                sidecar_credentials=rendered.sidecar_credentials,
            )
        component = self._resolve_component(language, version)
        return _ResolvedSpec(
            spec=self._build_spec(component),
            template_ref=None,
            component_refs=[component.key],
            component=component,
        )

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

        handle = await self._provisioner.acquire(resolved.spec)

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
        )
        session.add(sandbox_row)
        await session.flush()

        try:
            await self._provision_sidecars(handle, resolved.sidecar_components, resolved.sidecar_credentials)
            result = await self._provisioner.exec_batch(handle, batch_command)
        finally:
            # Graceful eradication (doc §4.1): always tear down the ephemeral sandbox,
            # whether sidecar provisioning or the run itself succeeded, failed, or
            # timed out.
            await self._teardown_sidecars(handle, resolved.sidecar_components)
            await self._provisioner.destroy(handle)

        session.add(
            Run(
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
        )
        sandbox_row.state = "terminated"
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
        tenant_id: str,
        user_id: str | None,
        session: AsyncSession,
    ) -> Sandbox:
        resolved = self._resolve_spec(language=language, version=version, template=template)
        handle = await self._provisioner.acquire(resolved.spec)

        try:
            await self._provision_sidecars(handle, resolved.sidecar_components, resolved.sidecar_credentials)
        except Exception:
            # Nothing's been persisted yet at this point — the caller has no id to
            # destroy this by later, so a provisioning failure must clean up here or
            # the sandbox (and its sidecar) leaks with no way to reach it again.
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
            persistent=False,
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

    async def destroy_sandbox(self, sandbox_id: str, tenant_id: str, session: AsyncSession) -> None:
        row = await self.get_sandbox(sandbox_id, tenant_id, session)
        if row.state == "terminated":
            return  # idempotent, mirrors Provisioner.destroy()'s own idempotency
        handle = self._handle_from_row(row)
        await self._teardown_sidecars(handle, self._sidecar_components_for_row(row))
        await self._provisioner.destroy(handle)
        row.state = "terminated"
        row.terminated_at = datetime.now(UTC)
        await session.commit()

    async def open_pty(self, sandbox_id: str, tenant_id: str, session: AsyncSession) -> PTYStream:
        """Interactive attach (doc §5.2, Phase 4) — the WS gateway's only touchpoint
        into Provisioner-land, keeping SandboxService the sole seam that talks to a
        Provisioner directly (doc §4.2's whole point)."""
        handle = await self._live_handle(sandbox_id, tenant_id, session)
        return await self._provisioner.attach(handle)

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
