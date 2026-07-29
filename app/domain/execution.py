"""Execution-time domain models: what a Provisioner is asked to do and what it returns.

Distinct from app/domain/manifests.py (which models the *declared* Component/Template
YAML) — these are the *resolved*, ready-to-run objects SandboxService hands to a
Provisioner (doc §4.2, §5).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SandboxState(StrEnum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    READY = "ready"
    ACTIVE = "active"
    IDLE = "idle"
    TERMINATING = "terminating"
    TERMINATED = "terminated"
    FAILED = "failed"


class WeightClass(StrEnum):
    LIGHT = "light"
    STANDARD = "standard"
    HEAVY = "heavy"


class ResourceSpec(BaseModel):
    cpu: str
    memory: str
    ephemeral_storage_mb: int | None = None


class SidecarPort(BaseModel):
    name: str
    container_port: int


class SidecarSpec(BaseModel):
    """One additional container composed into a sandbox alongside `main` (doc §3.3,
    §20 Phase 5) — e.g. a database. Reachable from `main` only over localhost (shared
    pod network namespace / Docker `network_mode: container:`), never externally (doc
    §3.3's `network.reachableFrom: same-pod-only`) — see the provisioners for how that
    reachability is actually realized on each backend.
    """

    name: str
    image: str
    env: dict[str, str] = Field(default_factory=dict)
    resources: ResourceSpec
    ports: list[SidecarPort] = Field(default_factory=list)
    writable_paths: list[str] = Field(default_factory=list)
    health_check: list[str] | None = None
    uid: int
    """The OS uid (and gid) the sidecar image's own process actually runs as — not
    necessarily _SANDBOX_UID (10001); a database image has its own expected user (e.g.
    official Postgres/MySQL/Redis images conventionally run as uid 999) that
    tmpfs/emptyDir ownership and the Kubernetes per-container securityContext must
    match, or the process can't write its own data directory. Meant to be confirmed
    per-image via a live `docker run --rm <image> id` probe (Phase 5's
    live-verification loop), not guessed and left unchecked."""
    max_processes: int = 256


class SandboxSpec(BaseModel):
    """Fully-resolved spec a Provisioner can realize directly — produced by
    SandboxService from a SandboxTemplate or an ad-hoc single-component request."""

    image: str
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    workdir: str = "/workspace"
    writable_paths: list[str] = Field(default_factory=lambda: ["/workspace", "/tmp"])
    read_only_root_filesystem: bool = True
    resources: ResourceSpec
    weight_class: WeightClass = WeightClass.LIGHT
    wall_clock_seconds: int = 60
    max_output_bytes: int = 5_000_000
    max_processes: int = 128
    labels: dict[str, str] = Field(default_factory=dict)
    sidecars: list[SidecarSpec] = Field(default_factory=list)
    workspace_id: str | None = None
    """Set only for a persistent sandbox (doc §10.2, Phase 7) — provisioners mount a
    durable volume/PVC keyed by this id at `workdir` instead of ephemeral tmpfs/
    emptyDir, so the same workspace survives across that workspace's own sandbox
    create/destroy cycles. `None` (the default, and the only value pooling/`execute()`
    ever use) means today's ephemeral-only behavior, unchanged."""
    workspace_size_mb: int | None = None
    """The owning Workspace's quota (doc §10.2) — only meaningful alongside
    `workspace_id`; sizes the Kubernetes PVC request (Docker named volumes have no
    size cap of their own, so this is a no-op there beyond documentation)."""
    node_selector: dict[str, str] = Field(default_factory=dict)
    """K8s-only heavy-weight-class node segregation (doc §4.3) — ignored by
    DockerProvisioner, which has no node concept. Populated by SandboxService from
    `settings.provisioner.heavy_node_selector` only when `weight_class == HEAVY`."""
    tolerations: list[dict[str, str]] = Field(default_factory=list)
    """Raw K8s Toleration dicts matching `node_selector` above — same K8s-only,
    heavy-only, config-driven story."""


class SandboxHandle(BaseModel):
    """Opaque reference returned by a Provisioner; only it knows how to act on it."""

    sandbox_id: str
    backend: str  # "docker" | "kubernetes"
    native_ref: str  # container id / pod name
    created_at: datetime
    sidecar_refs: dict[str, str] = Field(default_factory=dict)
    """Sidecar component name -> provisioner-native ref: a container id for Docker
    (each sidecar is its own container sharing main's network namespace), or the same
    name again for Kubernetes (sidecars are just other containers in the same Pod,
    addressed by name)."""
    persistent: bool = False
    """True when this handle backs a persistent-workspace sandbox (Phase 7) — the only
    thing `KubernetesProvisioner.destroy()` reads to decide whether to delete just the
    Pod (persistent: its namespace holds the durable PVC and must survive) or the
    whole namespace (ephemeral: today's behavior, unchanged). Docker's `destroy()`
    doesn't need this — a named volume already survives a plain container removal
    regardless (Docker's `v=True` on delete only reaps *anonymous* volumes)."""


class SandboxStatus(BaseModel):
    sandbox_id: str
    state: SandboxState
    detail: str | None = None


class NativeSandboxRef(BaseModel):
    """One live, sandbox-labeled native resource discovered directly from the
    provisioner backend (doc §4.1's "garbage-collects orphaned pods", Phase 7's
    reconciler) — independent of anything in Postgres, since the whole point is
    finding resources Postgres doesn't (or no longer) knows about."""

    sandbox_id: str
    native_ref: str
    created_at: datetime
    sidecar_refs: dict[str, str] = Field(default_factory=dict)


class BatchCommand(BaseModel):
    """One bundled batch execution request (doc §5.1) — stdin is entirely up front.

    Self-contained on purpose: a Provisioner.exec_batch(handle, command) call carries
    everything it needs (including limits) without having to remember the SandboxSpec
    a sandbox was originally acquired with.
    """

    command: list[str]
    stdin: str = ""
    files: dict[str, str] = Field(default_factory=dict)  # relative path -> content
    timeout_seconds: int = 60
    max_output_bytes: int = 5_000_000
    capture_variables: bool = False


VARIABLE_DUMP_PATH = "/tmp/.kubesandbox_vars.json"
"""Must match components/languages/python/runner.py's VAR_DUMP_PATH (doc §5.3)."""


class BatchRunResult(BaseModel):
    run_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    timed_out: bool = False
    variables: dict[str, Any] | None = None


class FileEntry(BaseModel):
    """One entry from Provisioner.list_tree() (doc §5.4)."""

    path: str  # relative to the requested root
    is_dir: bool
