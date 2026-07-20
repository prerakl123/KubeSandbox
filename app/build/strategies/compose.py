"""ComposeBuildStrategy (doc §8) — a kompose-style translator, scoped to what this
phase actually needs: build/tag every service in a docker-compose file that declares
a `build:` context, delegating the actual build to the same
`build_image_from_dockerfile` helper DockerfileBuildStrategy uses.

Known scope boundary (docs/TASK_CHECKLIST.md): this builds each declared service's
image — it does NOT auto-translate a multi-service compose file into SidecarSpecs.
Phase 5 already covers real sidecar composition via hand-authored SandboxTemplates;
re-deriving that automatically from a compose file is a separate, unrequested feature.
The "primary" service (the one whose ref becomes this build's Artifact) is the one
matching the component's own name, or the first declared service if none match; any
other built services are recorded in Artifact.metadata["services"] for future use.
"""

from __future__ import annotations

import yaml

from app.build.strategies.dockerfile import build_image_from_dockerfile
from app.core.errors import BuildError
from app.domain.build import Artifact, BuildContext
from app.domain.manifests import Component

_DEFAULT_COMPOSE_FILE = "docker-compose.yaml"


def parse_compose_services(raw: dict) -> dict[str, dict]:
    """Pure, unit-testable without touching Docker: the compose-YAML-shape parsing
    half of this strategy."""
    services = raw.get("services")
    if not isinstance(services, dict) or not services:
        raise BuildError("compose file declares no services")
    return services


def select_primary_service(services: dict[str, dict], component_name: str) -> str:
    if component_name in services:
        return component_name
    return next(iter(services))


class ComposeBuildStrategy:
    async def build(self, component: Component, ctx: BuildContext) -> Artifact:
        source = component.spec.source.compose
        filename = (source.file if source else None) or _DEFAULT_COMPOSE_FILE
        compose_path = ctx.component_dir / filename
        if not compose_path.is_file():
            raise BuildError(f"compose file {compose_path} not found")

        raw = yaml.safe_load(compose_path.read_text())
        services = parse_compose_services(raw)
        primary_name = select_primary_service(services, component.metadata.name)

        built_refs: dict[str, str] = {}
        for name, service in services.items():
            build_spec = service.get("build")
            if build_spec is not None:
                context_rel = build_spec if isinstance(build_spec, str) else build_spec.get("context", ".")
                dockerfile_rel = None if isinstance(build_spec, str) else build_spec.get("dockerfile")
                is_primary = name == primary_name
                local_tag = (
                    f"{ctx.image_repo}:{ctx.image_tag}"
                    if is_primary
                    else f"{ctx.image_repo}-{name}:{ctx.image_tag}"
                )
                await build_image_from_dockerfile(
                    ctx.component_dir,
                    context=context_rel,
                    dockerfile_path=dockerfile_rel,
                    local_tag=local_tag,
                    log=ctx.log,
                )
                built_refs[name] = local_tag
            elif "image" in service:
                built_refs[name] = service["image"]

        primary_ref = built_refs.get(primary_name)
        if primary_ref is None:
            raise BuildError(
                f"compose service {primary_name!r} declares neither `build` nor `image`"
            )

        other_services = {k: v for k, v in built_refs.items() if k != primary_name}
        return Artifact(kind="image", ref=primary_ref, metadata={"services": other_services})
