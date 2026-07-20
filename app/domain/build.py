"""Execution-time domain models for the build system (doc §8, roadmap Phase 6).

Distinct from app/domain/manifests.py's `ComponentSource` (the *declared* build
strategy in a component's YAML) — these are what a `BuildStrategy` actually produces
and needs to run, mirroring the SandboxSpec/BatchCommand split in app/domain/execution.py
between declared manifests and resolved, ready-to-run objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from app.domain.manifests import Component

ArtifactKind = Literal["image", "manifest"]


@dataclass(frozen=True)
class Artifact:
    """What a BuildStrategy hands back to BuildManager.

    `kind == "image"`: `ref` is the *local*, not-yet-pushed image tag (e.g.
    "kubesandbox/jq:1.0") — BuildManager pushes it via the configured
    ImageRegistryProvider and records the resolved, pullable ref separately.

    `kind == "manifest"`: `ref` is an object-store key holding rendered content (e.g.
    a helm-templated manifest) — there is no image to push, and no sandbox can run
    directly from it.
    """

    kind: ArtifactKind
    ref: str
    metadata: dict = field(default_factory=dict)
    """Strategy-specific extras that don't fit `ref` — e.g. ComposeBuildStrategy
    records OTHER services it built (beyond the one it returned as the primary
    artifact) under metadata["services"]: dict[str, str]."""


class ObjectStorageProvider(Protocol):
    """Forward-declared here (not imported from app.cloud.storage) to avoid a
    domain -> cloud import — app/domain/ models what strategies need, not who
    provides it. app/cloud/storage.py's real classes structurally satisfy this."""

    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...


@dataclass
class BuildContext:
    """Everything a BuildStrategy needs to do its work — analogous to RenderContext
    (app/extensions/hooks.py) for ComponentHook."""

    component_dir: Path
    """On-disk directory holding the component's Dockerfile/compose file/chart —
    see Registry.component_dirs (app/extensions/loader.py)."""
    build_id: str
    image_repo: str
    """Target repository for an image-producing strategy, e.g. "kubesandbox/jq"."""
    image_tag: str
    """Target tag, e.g. the component's version ("1.0")."""
    log: list[str] = field(default_factory=list)
    """Strategies append log lines here; BuildManager persists the tail as
    Build.log_excerpt after the build finishes (success or failure)."""
    object_storage: ObjectStorageProvider | None = None
    """Only None if no ObjectStorageProvider is configured — PipelineBuildStrategy's
    cache and HelmChartStrategy's artifact upload both require it and raise a clear
    BuildError if it's missing rather than silently skipping their contract."""


class BuildStrategy(Protocol):
    """One per Component.spec.source.type (doc §8) — BuildManager dispatches to the
    matching strategy by a fixed, built-in map (no plugin loading, unlike
    ComponentHook: these four are internal, not user-pluggable per component)."""

    async def build(self, component: Component, ctx: BuildContext) -> Artifact: ...
