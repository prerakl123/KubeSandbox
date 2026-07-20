"""HelmChartStrategy (doc §8) — renders a chart for a "service"-category component,
storing the rendered manifest in object storage.

Known scope boundary (docs/TASK_CHECKLIST.md): this renders and stores the manifest —
it does NOT wire it into a running sandbox pod. No existing doc section describes how
a helm-rendered service composes into SidecarSpec (Phase 5's sidecars are all
`source.type: image`); that's a real, unaddressed gap, flagged rather than faked.

`helm` is an external CLI prerequisite (like `kubectl`/`kind` were for Phase 3) — a
clear BuildError if it's missing, matching doc's "fail loudly" cloud-stub philosophy
even though this isn't a cloud stub.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

import yaml

from app.core.errors import BuildError
from app.domain.build import Artifact, BuildContext
from app.domain.manifests import Component

_DEFAULT_CHART_DIR = "chart"


class HelmChartStrategy:
    async def build(self, component: Component, ctx: BuildContext) -> Artifact:
        if shutil.which("helm") is None:
            raise BuildError(
                "`helm` binary not found on PATH — required to build helm-sourced "
                "components (optional local prerequisite, see README.md)"
            )
        if ctx.object_storage is None:
            raise BuildError(
                "helm-sourced components require an ObjectStorageProvider to store "
                "the rendered manifest — set object_storage in config/settings (doc §9)"
            )

        source = component.spec.source.helm
        chart_dir = ctx.component_dir / ((source.chart if source else None) or _DEFAULT_CHART_DIR)
        if not chart_dir.is_dir():
            raise BuildError(f"helm chart directory {chart_dir} not found")

        values_path = None
        if source and source.values:
            fd, values_path = tempfile.mkstemp(suffix=".yaml")
            with os.fdopen(fd, "w") as f:
                yaml.safe_dump(source.values, f)

        try:
            command = ["helm", "template", component.metadata.name, str(chart_dir)]
            if values_path:
                command += ["-f", values_path]
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
        finally:
            if values_path:
                os.unlink(values_path)

        if process.returncode != 0:
            raise BuildError(f"helm template failed: {stderr.decode(errors='replace')}")

        rendered = stdout.decode()
        ctx.log.append(rendered)

        key = f"helm-artifacts/{component.metadata.name}/{component.metadata.version}/manifest.yaml"
        await ctx.object_storage.put(key, rendered.encode())
        return Artifact(kind="manifest", ref=key)
