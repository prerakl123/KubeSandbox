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


class SandboxHandle(BaseModel):
    """Opaque reference returned by a Provisioner; only it knows how to act on it."""

    sandbox_id: str
    backend: str  # "docker" | "kubernetes"
    native_ref: str  # container id / pod name
    created_at: datetime


class SandboxStatus(BaseModel):
    sandbox_id: str
    state: SandboxState
    detail: str | None = None


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
