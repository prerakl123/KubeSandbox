"""Resolves a language/template request into a SandboxSpec + BatchCommand, drives an
ephemeral sandbox through acquire -> exec_batch -> destroy, and persists the run.

Phase 1 scope only: single ad-hoc language component (no SandboxTemplate composition
yet — that's Phase 2), always ephemeral (no pooling/recycling — Phase 7).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ComponentNotFoundError, KubeSandboxError
from app.domain.execution import BatchCommand, BatchRunResult, ResourceSpec, SandboxSpec, WeightClass
from app.domain.manifests import Component
from app.extensions.loader import Registry
from app.persistence.models import Run, Sandbox
from app.provisioners.base import Provisioner


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

    async def execute(
        self,
        *,
        language: str,
        code: str,
        version: str | None = None,
        stdin: str = "",
        tenant_id: str,
        user_id: str | None,
        session: AsyncSession,
    ) -> BatchRunResult:
        component = self._resolve_component(language, version)
        spec = self._build_spec(component)
        batch_command = self._build_batch_command(component, code, stdin)

        handle = await self._provisioner.acquire(spec)

        sandbox_row = Sandbox(
            id=handle.sandbox_id,
            tenant_id=tenant_id,
            user_id=user_id,
            component_refs=[component.key],
            backend=handle.backend,
            native_ref=handle.native_ref,
            state="active",
            weight_class=spec.weight_class.value,
            persistent=False,
        )
        session.add(sandbox_row)
        await session.flush()

        try:
            result = await self._provisioner.exec_batch(handle, batch_command)
        finally:
            # Graceful eradication (doc §4.1): always tear down the ephemeral sandbox,
            # whether the run succeeded, failed, or timed out.
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
