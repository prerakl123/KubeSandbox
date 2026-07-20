"""DockerfileBuildStrategy (doc §8) — builds against the local Docker daemon.

Real Kaniko/BuildKit-in-Kubernetes for `aks-prod` is NOT implemented this phase — a
deliberate, documented scope cut (see docs/TASK_CHECKLIST.md's Phase 6 section): it
can't be exercised live in this environment (no AKS, no cluster), and building it
untested would be a bigger version of the same honesty gap Phase 3 already carries
for gVisor. `build_image_from_dockerfile` is also reused directly by
ComposeBuildStrategy, which needs the identical "tar a context dir, build via
aiodocker" step per service.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path

import aiodocker
from aiodocker.exceptions import DockerError

from app.core.errors import BuildError
from app.domain.build import Artifact, BuildContext
from app.domain.manifests import Component


def _tar_context(context_dir: Path) -> bytes:
    """Pure and separately unit-testable — the only part of this strategy that
    doesn't need a real Docker daemon to exercise."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(context_dir, arcname=".")
    return buffer.getvalue()


async def build_image_from_dockerfile(
    component_dir: Path,
    *,
    context: str | None,
    dockerfile_path: str | None,
    local_tag: str,
    log: list[str],
) -> str:
    """Shared by DockerfileBuildStrategy and ComposeBuildStrategy (per-service build).
    `context`/`dockerfile_path` mirror ComponentSource.dockerfile's own field names,
    both relative to `component_dir`. Returns `local_tag` unchanged on success — the
    caller already knows the tag it asked for; this just confirms the daemon built it.
    """
    context_dir = component_dir / context if context else component_dir
    if not context_dir.is_dir():
        raise BuildError(f"dockerfile build context {context_dir} is not a directory")

    tar_bytes = await asyncio.to_thread(_tar_context, context_dir)

    docker = aiodocker.Docker()
    try:
        chunks = await docker.images.build(
            fileobj=io.BytesIO(tar_bytes),
            encoding="gzip",
            path_dockerfile=dockerfile_path,
            tag=local_tag,
            rm=True,
        )
    except DockerError as exc:
        raise BuildError(f"docker build failed for {local_tag}: {exc}") from exc
    finally:
        await docker.close()

    for chunk in chunks:
        if isinstance(chunk, dict) and "stream" in chunk:
            log.append(str(chunk["stream"]).rstrip("\n"))

    return local_tag


class DockerfileBuildStrategy:
    async def build(self, component: Component, ctx: BuildContext) -> Artifact:
        source = component.spec.source.dockerfile
        local_tag = f"{ctx.image_repo}:{ctx.image_tag}"
        await build_image_from_dockerfile(
            ctx.component_dir,
            context=source.context if source else None,
            dockerfile_path=source.path if source else None,
            local_tag=local_tag,
            log=ctx.log,
        )
        return Artifact(kind="image", ref=local_tag)
