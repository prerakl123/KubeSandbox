"""Pure SandboxTemplate -> SandboxSpec composition (doc §3.4, roadmap Phase 2).

Renders a SandboxTemplate's base + declared components into one SandboxSpec a
Provisioner can acquire() directly. No I/O, no DB session — SandboxService (running a
template) and TemplateService (just inspecting/validating one) both call this the same
way, so "compose a template" and "run a template" can never drift apart.

Real multi-image merging (a template mixing components whose golden images genuinely
differ) is BuildManager's job (doc §8, roadmap Phase 6) — until then, every
"mainTool"-kind component in a template (the base included) must resolve to the exact
same image reference, since a Provisioner today only ever runs one container per
sandbox (Phase 5 adds sidecars). That invariant is enforced here, not silently ignored:
a template whose components need different images fails loudly with a message pointing
at Phase 6, instead of quietly picking one image and dropping the rest of what the
template declared.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.errors import KubeSandboxError
from app.domain.execution import ResourceSpec, SandboxSpec, SidecarPort, SidecarSpec, WeightClass
from app.domain.manifests import Component, SandboxTemplate
from app.extensions.loader import Registry
from app.services.credentials import DbCredentials, generate_db_credentials

_WEIGHT_ORDER = {WeightClass.LIGHT: 0, WeightClass.STANDARD: 1, WeightClass.HEAVY: 2}


@dataclass
class RenderedTemplateSpec:
    template_key: str
    sandbox_spec: SandboxSpec
    main_components: list[Component]
    """kind == mainTool — actually runnable in today's single-container Provisioner."""
    sidecar_components: list[Component]
    """kind != mainTool — materialized into sandbox_spec.sidecars as real additional
    containers (doc §4.3/§20 checklist, Phase 5)."""
    sidecar_credentials: dict[str, DbCredentials] = field(default_factory=dict)
    """Sidecar component name -> the DbCredentials generated for it, keyed the same
    way SandboxHandle.sidecar_refs is. SandboxService needs the SAME credentials
    object after acquire() to hand to that sidecar's on_provision hook — regenerating
    fresh ones there would create a role/password the hook creates but that doesn't
    match what's already baked into main's DATABASE_URL-style env var."""
    ttl_idle: str = ""
    ttl_max: str = ""


def _component_image_ref(component: Component) -> str | None:
    source = component.spec.source
    if source.type != "image" or source.image is None:
        return None
    return f"{source.image.repository}:{source.image.tag}"


def _build_dsn(protocol: str, credentials: DbCredentials, port: int) -> str:
    # Redis addresses databases by numeric index (0-15), not by name — credentials.
    # database ("sandbox") is a SQL-style logical db name that doesn't apply there.
    path = "0" if protocol == "redis" else credentials.database
    return f"{protocol}://{credentials.role}:{credentials.password}@localhost:{port}/{path}"


def _build_sidecar_spec(component: Component, credentials: DbCredentials | None) -> SidecarSpec:
    runtime = component.spec.runtime
    access = component.spec.access
    if runtime.uid is None:
        raise KubeSandboxError(
            f"sidecar component {component.key} (runtime.kind={runtime.kind!r}) declares "
            "no runtime.uid — the OS uid/gid its image's own process runs as, needed for "
            "tmpfs/emptyDir ownership and the Kubernetes per-container securityContext "
            "(see SidecarSpec.uid's docstring)"
        )
    source = component.spec.source
    if source.type != "image" or source.image is None:
        raise KubeSandboxError(
            f"sidecar component {component.key} uses source.type={source.type!r}; only "
            "prebuilt images are runnable until BuildManager lands (roadmap Phase 6)"
        )
    image = f"{source.image.repository}:{source.image.tag}"

    env = {var.name: var.value for var in runtime.env}
    if credentials is not None and access.database is not None and access.database.adminPasswordEnv:
        env[access.database.adminPasswordEnv] = credentials.admin_password

    return SidecarSpec(
        name=component.metadata.name,
        image=image,
        env=env,
        resources=ResourceSpec(cpu=runtime.resources.limits.cpu, memory=runtime.resources.limits.memory),
        ports=[SidecarPort(name=p.name, container_port=p.containerPort) for p in runtime.ports],
        writable_paths=list(access.filesystem.writablePaths),
        health_check=list(runtime.healthCheck.exec) if runtime.healthCheck else None,
        uid=runtime.uid,
        max_processes=access.limits.processes,
    )


def render_template(registry: Registry, template: SandboxTemplate) -> RenderedTemplateSpec:
    base = registry.resolve_component_ref(template.spec.base.ref)
    components = [registry.resolve_component_ref(ref.ref) for ref in template.spec.components]
    all_components = [base, *components]

    main_components = [c for c in all_components if c.spec.runtime.kind == "mainTool"]
    sidecar_components = [c for c in all_components if c.spec.runtime.kind != "mainTool"]

    image_refs = {ref for c in main_components if (ref := _component_image_ref(c)) is not None}
    if not image_refs:
        raise KubeSandboxError(
            f"template {template.key} has no mainTool-kind component with a runnable "
            "image source (source.type == 'image'); other build strategies aren't "
            "runnable until BuildManager lands (roadmap Phase 6)"
        )
    if len(image_refs) > 1:
        raise KubeSandboxError(
            f"template {template.key} composes mainTool components across "
            f"{len(image_refs)} distinct images ({sorted(image_refs)}) — merging "
            "separate golden images into one running container needs BuildManager "
            "(roadmap Phase 6); today every mainTool-kind component in a template "
            "must share one pre-baked image"
        )
    image = next(iter(image_refs))

    env: dict[str, str] = {}
    writable_paths: list[str] = []
    workdir: str | None = None
    read_only_root_filesystem = True
    # Only main_components here, not all_components: a sidecar's own env (e.g. a DB
    # image's bootstrap admin password) and writable paths (e.g. its data directory)
    # belong on ITS OWN container, never leaked into main's — see _build_sidecar_spec
    # below for where a sidecar's env/writable_paths actually get materialized.
    for component in main_components:
        for var in component.spec.runtime.env:
            env[var.name] = var.value  # last component wins on key collision
        access = component.spec.access
        for path in access.filesystem.writablePaths:
            if path not in writable_paths:
                writable_paths.append(path)
        if not access.filesystem.readOnlyRootFilesystem:
            read_only_root_filesystem = False
        if workdir is None:
            workdir = access.filesystem.workdir

    sidecar_specs: list[SidecarSpec] = []
    sidecar_credentials: dict[str, DbCredentials] = {}
    for component in sidecar_components:
        credentials = None
        if component.spec.access.database is not None:
            credentials = generate_db_credentials(component)
            sidecar_credentials[component.metadata.name] = credentials
            service = component.spec.provides.service
            if service is not None and service.dsnEnv:
                env[service.dsnEnv] = _build_dsn(service.protocol, credentials, service.port)
        sidecar_specs.append(_build_sidecar_spec(component, credentials))

    weight_class = (
        WeightClass(template.spec.weightClass)
        if template.spec.weightClass
        else max(
            (WeightClass(c.spec.runtime.weightClass) for c in all_components),
            key=lambda w: _WEIGHT_ORDER[w],
        )
    )

    sandbox_spec = SandboxSpec(
        image=image,
        command=["sleep", "infinity"],  # acquire() always launches the idle keep-alive
        env=env,
        workdir=workdir or "/workspace",
        writable_paths=writable_paths or ["/workspace", "/tmp"],
        read_only_root_filesystem=read_only_root_filesystem,
        resources=ResourceSpec(
            cpu=template.spec.resources.cpu,
            memory=template.spec.resources.memory,
            ephemeral_storage_mb=template.spec.resources.ephemeralStorageMB,
        ),
        weight_class=weight_class,
        wall_clock_seconds=max(c.spec.access.limits.wallClockSeconds for c in all_components),
        max_output_bytes=max(c.spec.access.limits.outputBytes for c in all_components),
        max_processes=max(c.spec.access.limits.processes for c in all_components),
        labels={
            "io.kubesandbox.template": template.key,
            "io.kubesandbox.components": ",".join(c.key for c in all_components),
        },
        sidecars=sidecar_specs,
    )

    return RenderedTemplateSpec(
        template_key=template.key,
        sandbox_spec=sandbox_spec,
        main_components=main_components,
        sidecar_components=sidecar_components,
        sidecar_credentials=sidecar_credentials,
        ttl_idle=template.spec.ttl.idle,
        ttl_max=template.spec.ttl.max,
    )
