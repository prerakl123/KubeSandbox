"""PipelineBuildStrategy (doc §8) — runs a component's declared ordered steps, then
packages the result the same way DockerfileBuildStrategy does.

Doc's own example has a `package` step invoke `kaniko --destination $IMAGE` directly;
since real Kaniko is out of scope this phase (see dockerfile.py's module docstring —
same Kaniko-deferred decision applies here) and shelling out to the `docker` CLI would
introduce a prerequisite this codebase otherwise avoids entirely (every other Docker
interaction goes through aiodocker, never the CLI), declared `steps` here are
pre-build hooks (fetch/prepare/lint) run via subprocess with `$IMAGE` (and
`$COMPONENT_NAME`/`$COMPONENT_VERSION`) available in their environment for scripts
that want to reference the target ref — the actual image packaging is then performed
by this strategy via the shared `build_image_from_dockerfile` helper, mirroring what
Kaniko would do in `aks-prod`.

The step-runner is injectable (`step_runner=`) so unit tests can substitute a fake
recorder instead of a real shell — the same "swap the I/O boundary" pattern
`FakeProvisioner` uses for `SandboxService` (tests/unit/fakes.py).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.build.strategies.dockerfile import build_image_from_dockerfile
from app.core.errors import BuildError
from app.domain.build import Artifact, BuildContext
from app.domain.manifests import Component

StepRunner = Callable[[str, Path, dict[str, str], list[str]], Awaitable[None]]


async def run_step_subprocess(command: str, cwd: Path, env: dict[str, str], log: list[str]) -> None:
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        env={**os.environ, **env},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    if stdout:
        log.append(stdout.decode(errors="replace"))
    if process.returncode != 0:
        raise BuildError(f"pipeline step failed (exit {process.returncode}): {command!r}")


def _cache_object_key(cache_key: str) -> str:
    return f"pipeline-cache/{cache_key}.json"


class PipelineBuildStrategy:
    def __init__(self, step_runner: StepRunner = run_step_subprocess) -> None:
        self._step_runner = step_runner

    async def _cached_artifact(self, object_storage, cache_key: str) -> Artifact | None:
        try:
            data = await object_storage.get(_cache_object_key(cache_key))
        except KeyError:
            return None
        cached = json.loads(data)
        return Artifact(kind="image", ref=cached["image_ref"], metadata={"cache_hit": True})

    async def build(self, component: Component, ctx: BuildContext) -> Artifact:
        source = component.spec.source.pipeline
        cache_key: str | None = None

        if source.cache is not None and source.cache.key:
            if ctx.object_storage is None:
                raise BuildError(
                    "pipeline declares a build cache but no ObjectStorageProvider is "
                    "configured — set object_storage in config/settings (doc §9)"
                )
            cache_key = source.cache.key.format(
                name=component.metadata.name, version=component.metadata.version
            )
            cached = await self._cached_artifact(ctx.object_storage, cache_key)
            if cached is not None:
                ctx.log.append(f"cache hit for {cache_key!r} — skipping {len(source.steps)} step(s)")
                return cached

        local_tag = f"{ctx.image_repo}:{ctx.image_tag}"
        env = {
            "IMAGE": local_tag,
            "COMPONENT_NAME": component.metadata.name,
            "COMPONENT_VERSION": component.metadata.version,
        }
        for step in source.steps:
            ctx.log.append(f"[{step.name}] $ {step.run}")
            await self._step_runner(step.run, ctx.component_dir, env, ctx.log)

        await build_image_from_dockerfile(
            ctx.component_dir,
            context=None,
            dockerfile_path=None,
            local_tag=local_tag,
            log=ctx.log,
        )

        if cache_key is not None and ctx.object_storage is not None:
            await ctx.object_storage.put(
                _cache_object_key(cache_key), json.dumps({"image_ref": local_tag}).encode()
            )

        return Artifact(kind="image", ref=local_tag)
