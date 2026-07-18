"""Shared in-memory manifest builders for Phase 2 unit tests (template rendering,
entitlements, registry/template services) — avoids re-deriving a minimal-but-valid
Component/SandboxTemplate by hand in every test module.
"""

from __future__ import annotations

from app.domain.manifests import (
    Component,
    ComponentAccess,
    ComponentMetadata,
    ComponentProvides,
    ComponentRuntime,
    ComponentSource,
    ComponentSpec,
    ContainerPort,
    DatabaseAccess,
    EnvVar,
    ExecutionLimitsSpec,
    FilesystemAccess,
    HealthCheck,
    ImageSource,
    ResourceQuantities,
    ResourceRequirements,
    SandboxTemplate,
    SandboxTemplateSpec,
    ServiceSpec,
    TemplateBase,
    TemplateComponentRef,
    TemplateMetadata,
    TemplateResources,
    TTLSpec,
)


def make_component(
    name: str,
    version: str,
    *,
    category: str = "language",
    image_repo: str = "kubesandbox/x",
    image_tag: str = "1",
    kind: str = "mainTool",
    weight: str = "light",
    env: list[EnvVar] | None = None,
    writable_paths: list[str] | None = None,
    read_only_root_filesystem: bool = True,
    default_run: str | None = None,
    uid: int | None = None,
    database: DatabaseAccess | None = None,
    service: ServiceSpec | None = None,
    health_check: list[str] | None = None,
    ports: list[ContainerPort] | None = None,
) -> Component:
    return Component(
        apiVersion="kubesandbox.io/v1",
        kind="Component",
        metadata=ComponentMetadata(name=name, version=version, category=category),
        spec=ComponentSpec(
            source=ComponentSource(
                type="image", image=ImageSource(repository=image_repo, tag=image_tag)
            ),
            provides=ComponentProvides(
                fileExtensions=[".txt"] if default_run else [],
                defaultRun=default_run,
                service=service,
            ),
            runtime=ComponentRuntime(
                kind=kind,
                weightClass=weight,
                resources=ResourceRequirements(
                    requests=ResourceQuantities(cpu="50m", memory="64Mi"),
                    limits=ResourceQuantities(cpu="200m", memory="128Mi"),
                ),
                env=env or [],
                ports=ports or [],
                healthCheck=HealthCheck(exec=health_check) if health_check else None,
                uid=uid,
            ),
            access=ComponentAccess(
                filesystem=FilesystemAccess(
                    workdir="/workspace",
                    writablePaths=writable_paths or ["/workspace"],
                    readOnlyRootFilesystem=read_only_root_filesystem,
                ),
                limits=ExecutionLimitsSpec(processes=16, outputBytes=100_000, wallClockSeconds=10),
                database=database,
            ),
        ),
    )


def make_template(
    name: str,
    version: str,
    *,
    base_ref: str,
    component_refs: list[str],
    weight_class: str | None = None,
) -> SandboxTemplate:
    return SandboxTemplate(
        apiVersion="kubesandbox.io/v1",
        kind="SandboxTemplate",
        metadata=TemplateMetadata(name=name, version=version),
        spec=SandboxTemplateSpec(
            base=TemplateBase(ref=base_ref),
            components=[TemplateComponentRef(ref=ref) for ref in component_refs],
            weightClass=weight_class,
            resources=TemplateResources(cpu="500m", memory="256Mi"),
            ttl=TTLSpec(idle="15m", max="2h"),
        ),
    )
