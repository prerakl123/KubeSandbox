"""Typed models for Component and SandboxTemplate manifests (docs §3.2-§3.4).

These mirror schemas/component.schema.json and schemas/template.schema.json field for
field. JSON Schema catches structural/authoring mistakes with clear paths; pydantic then
gives the rest of the app typed objects to work with. Kept intentionally permissive on
optional fields (mirrors the schema's optionality) — required-ness lives in the schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["language", "database", "tool", "service", "base", "build-strategy"]
SourceType = Literal["image", "dockerfile", "compose", "pipeline", "helm"]
RuntimeKind = Literal["mainTool", "sidecar", "init", "ephemeral"]
WeightClass = Literal["light", "standard", "heavy"]
Environment = Literal["local", "aks-prod"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImageSource(_Strict):
    repository: str
    tag: str


class DockerfileSource(_Strict):
    context: str | None = None
    path: str | None = None


class ComposeSource(_Strict):
    file: str | None = None


class PipelineStep(_Strict):
    name: str
    run: str


class PipelineCache(_Strict):
    key: str | None = None
    store: Literal["object"] | None = None


class PipelineSource(_Strict):
    steps: list[PipelineStep]
    cache: PipelineCache | None = None


class HelmSource(_Strict):
    chart: str | None = None
    values: dict | None = None


class ComponentSource(_Strict):
    type: SourceType
    image: ImageSource | None = None
    dockerfile: DockerfileSource | None = None
    compose: ComposeSource | None = None
    pipeline: PipelineSource | None = None
    helm: HelmSource | None = None


class BatchRunnerSpec(_Strict):
    entrypoint: str
    supportsVariableDump: bool = False
    stdinMode: Literal["upfront"] = "upfront"


class ServiceSpec(_Strict):
    protocol: str
    port: int
    dsnEnv: str | None = None


class ComponentProvides(_Strict):
    languageId: str | None = None
    commands: list[str] = Field(default_factory=list)
    fileExtensions: list[str] = Field(default_factory=list)
    versionCommand: str | None = None
    defaultRun: str | None = None
    batchRunner: BatchRunnerSpec | None = None
    service: ServiceSpec | None = None


class ResourceQuantities(_Strict):
    cpu: str
    memory: str


class ResourceRequirements(_Strict):
    requests: ResourceQuantities
    limits: ResourceQuantities


class EnvVar(_Strict):
    name: str
    value: str


class VolumeMount(_Strict):
    name: str
    mountPath: str


class ContainerPort(_Strict):
    name: str
    containerPort: int


class HealthCheck(_Strict):
    exec: list[str] = Field(default_factory=list)


class ComponentRuntime(_Strict):
    kind: RuntimeKind
    weightClass: WeightClass = "light"
    resources: ResourceRequirements
    env: list[EnvVar] = Field(default_factory=list)
    volumeMounts: list[VolumeMount] = Field(default_factory=list)
    ports: list[ContainerPort] = Field(default_factory=list)
    healthCheck: HealthCheck | None = None


class NetworkAccess(_Strict):
    egress: Literal["intent-only", "denied"] = "denied"
    reason: str | None = None
    reachableFrom: Literal["same-pod-only"] | None = None


class PackageInstall(_Strict):
    enabled: bool = False
    source: Literal["mirror"] | None = None
    mirror: str | None = None
    denylist: list[str] = Field(default_factory=list)
    allowlist: list[str] = Field(default_factory=list)


class PackageAccess(_Strict):
    manager: str | None = None
    install: PackageInstall | None = None


class FilesystemAccess(_Strict):
    workdir: str
    writablePaths: list[str]
    readOnlyRootFilesystem: bool = True


class ExecutionLimitsSpec(_Strict):
    processes: int
    outputBytes: int
    wallClockSeconds: int


class DatabaseAccessLimits(_Strict):
    maxConnections: int | None = None
    statementTimeout: str | None = None
    maxDbSizeMB: int | None = None


class DatabaseAccess(_Strict):
    superuser: Literal[False] = False
    role: str | None = None
    grants: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    limits: DatabaseAccessLimits | None = None


class ComponentAccess(_Strict):
    network: NetworkAccess = Field(default_factory=NetworkAccess)
    packages: PackageAccess | None = None
    filesystem: FilesystemAccess
    limits: ExecutionLimitsSpec
    database: DatabaseAccess | None = None


class HookSpec(_Strict):
    module: str


class CompatibilitySpec(_Strict):
    environments: list[Environment] = Field(default_factory=lambda: ["local", "aks-prod"])
    runtimeClass: dict[str, str] = Field(default_factory=dict)


class ComponentSpec(_Strict):
    source: ComponentSource
    provides: ComponentProvides = Field(default_factory=ComponentProvides)
    runtime: ComponentRuntime
    access: ComponentAccess
    requires: list[str] = Field(default_factory=list)
    hooks: HookSpec | None = None
    compatibility: CompatibilitySpec = Field(default_factory=CompatibilitySpec)


class ComponentMetadata(_Strict):
    name: str
    version: str
    category: Category
    displayName: str | None = None
    description: str | None = None


class Component(_Strict):
    apiVersion: Literal["kubesandbox.io/v1"]
    kind: Literal["Component"]
    metadata: ComponentMetadata
    spec: ComponentSpec

    @property
    def key(self) -> str:
        return f"{self.metadata.name}@{self.metadata.version}"


class TemplateComponentRef(_Strict):
    ref: str


class TemplateBase(_Strict):
    ref: str


class WorkspaceSpec(_Strict):
    persistent: bool
    sizeMB: int | None = None


class TemplateResources(_Strict):
    cpu: str
    memory: str
    ephemeralStorageMB: int | None = None


class TTLSpec(_Strict):
    idle: str
    max: str


class SandboxTemplateSpec(_Strict):
    base: TemplateBase
    components: list[TemplateComponentRef]
    weightClass: WeightClass | None = None
    workspace: WorkspaceSpec | None = None
    resources: TemplateResources
    ttl: TTLSpec
    overrides: dict = Field(default_factory=dict)


class TemplateMetadata(_Strict):
    name: str
    version: str
    displayName: str | None = None
    description: str | None = None


class SandboxTemplate(_Strict):
    apiVersion: Literal["kubesandbox.io/v1"]
    kind: Literal["SandboxTemplate"]
    metadata: TemplateMetadata
    spec: SandboxTemplateSpec

    @property
    def key(self) -> str:
        return f"{self.metadata.name}@{self.metadata.version}"
