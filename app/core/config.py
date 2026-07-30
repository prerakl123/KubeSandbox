"""Layered configuration: code defaults <- config/settings/<env>.yaml <- env vars.

APP_ENV selects one of exactly two profiles: "local" or "aks-prod" (see
docs/ARCHITECTURE_AND_PLAN.md §7 — there is no separate "dev" profile by design).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

AppEnv = Literal["local", "aks-prod"]

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config" / "settings"

# APP_ENV has to be known before Settings is constructed (it picks the yaml file),
# so it's read directly from the environment rather than through pydantic-settings.
_APP_ENV: AppEnv = os.environ.get("KUBESANDBOX_APP_ENV", "local")  # type: ignore[assignment]
if _APP_ENV not in ("local", "aks-prod"):
    raise RuntimeError(
        f"KUBESANDBOX_APP_ENV must be 'local' or 'aks-prod', got {_APP_ENV!r}"
    )
_YAML_FILE = CONFIG_DIR / f"{_APP_ENV}.yaml"


class DatabaseSettings(BaseModel):
    dsn: str = "postgresql+asyncpg://kubesandbox:kubesandbox@localhost:5432/kubesandbox"
    pool_size: int = 10


class RedisSettings(BaseModel):
    url: str = "redis://localhost:6379/0"


class ObjectStorageSettings(BaseModel):
    # "aws"/"gcp" are selectable so a misconfiguration fails loudly at startup
    # (constructing the stub provider) rather than silently — doc §9's stub
    # philosophy, not something a real environment is expected to actually pick.
    provider: Literal["minio", "azure_blob", "aws", "gcp"] = "minio"
    endpoint: str = "http://localhost:9000"
    access_key: str = "kubesandbox"
    secret_key: str = "kubesandbox-secret"
    bucket: str = "kubesandbox"


class ImageRegistrySettings(BaseModel):
    provider: Literal["local", "acr", "aws", "gcp"] = "local"
    endpoint: str = "localhost:5000"


class SecretsSettings(BaseModel):
    provider: Literal["dotenv", "azure_keyvault"] = "dotenv"
    vault_url: str | None = None


class ProvisionerSettings(BaseModel):
    backend: Literal["docker", "kubernetes"] = "docker"
    runtime_class: str | None = None  # e.g. "gvisor" on aks-prod
    kubeconfig_path: str | None = None
    namespace_prefix: str = "kubesandbox-sb-"
    heavy_node_selector: dict[str, str] = Field(default_factory=dict)
    """K8s-only (doc §4.3): schedules `heavy` weight-class pods onto a segregated node
    pool via `nodeSelector`. Empty (the `local` default) means no selector is set —
    every pod lands on whatever node kind/AKS picks, same as today. Real segregation
    on `aks-prod` also needs a matching `heavy_tolerations` entry for whatever taint
    that node pool carries; both are config, not code, so an admin can repoint them at
    a real node pool without a redeploy."""
    heavy_tolerations: list[dict[str, str]] = Field(default_factory=list)
    """Each entry is a raw K8s Toleration dict (`key`/`operator`/`value`/`effect`) —
    passed straight through to `V1Toleration(**entry)`."""


class PoolSettings(BaseModel):
    """Warm-pool sizing per weight class (doc §4.3). Disabled by default in local."""

    enabled: bool = False
    light_pool_size: int = 0
    standard_pool_size: int = 0
    heavy_pool_size: int = 0
    heavy_max_concurrent: int | None = None
    """Doc §4.3: heavy templates "must not starve light ones". On `aks-prod` that's a
    real, separate node pool (`heavy_node_selector`/`heavy_tolerations` above); on
    `local` there's only one Docker host, so this caps how many `heavy` sandboxes may
    run concurrently via an in-process semaphore (doc §7's own table: "a separate
    resource budget/queue in local"). `None` = unbounded. Only meaningful with 1
    control-plane replica (doc §7: local always runs exactly 1) — a semaphore can't
    cap anything cluster-wide across replicas, which is exactly why `aks-prod` uses
    real node-pool segregation instead of this."""


class WorkspaceSettings(BaseModel):
    """Persistent workspace quota/retention defaults (doc §10.2)."""

    persistence_enabled: bool = False
    default_quota_mb: int = 10 * 1024  # 10 GiB
    idle_retention_days: int = 30
    archive_grace_days: int = 60
    max_lifetime_days: int = 365


class TTLSettings(BaseModel):
    """Sandbox idle/max TTL defaults (doc §4.1) for ad-hoc sandboxes that weren't
    created from a SandboxTemplate — a template declares its own `spec.ttl` (doc
    §3.4), but a bare `language=`/`version=` request (Phase 1's `execute()`/Phase 4's
    `create_sandbox()` path) has no template to read one from."""

    default_idle_seconds: int = 900  # 15m, matches templates/base-dev-lab.yaml's own default
    default_max_seconds: int = 7_200  # 2h, ditto


class ReconcilerSettings(BaseModel):
    """The dedicated reconciler worker (doc §4.1, §20 Phase 7) — a separate process
    from the API, per doc's own wording, not an in-API background task."""

    interval_seconds: int = 30
    orphan_grace_seconds: int = 120
    """A provisioner-native resource (container/namespace) carrying a
    `io.kubesandbox.sandbox-id` label with no matching, non-terminated `Sandbox` row is
    an orphan — but only once it's older than this grace window, so a sandbox that's
    mid-`acquire()` (row not committed yet) isn't mistaken for one."""


class ExecutionLimits(BaseModel):
    default_wall_clock_seconds: int = 60
    max_output_bytes: int = 5_000_000
    max_processes: int = 128


class BillingSettings(BaseModel):
    default_mode: Literal["credit", "payg"] = "credit"
    enabled: bool = False
    """Opt-in like `pool.enabled`/`workspace.persistence_enabled` before it (doc
    §4.3/§10.2's own precedent): false leaves SandboxService built with
    `billing_service=None`, so authorize()/record_usage() are skipped entirely — zero
    behavior change for a deployment that hasn't turned this on. Deliberately false in
    both `local.yaml` and `aks-prod.yaml` — flipping it on for real requires funding
    tenant wallets and configuring `pricing_rules` first (doc §13's admin APIs), or a
    fresh credit-mode tenant's zero balance blocks every sandbox creation outright."""


class AuthSettings(BaseModel):
    # Convenience-only escape hatch for local dev so /execute is reachable without
    # standing up an IdP first. Guarded below: forbidden outside "local".
    disabled: bool = False
    jwt_secret: str = "change-me-in-local-dev-only"
    oidc_issuer: str | None = None


class Settings(BaseSettings):
    app_env: AppEnv = _APP_ENV
    debug: bool = True

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    object_storage: ObjectStorageSettings = Field(default_factory=ObjectStorageSettings)
    image_registry: ImageRegistrySettings = Field(default_factory=ImageRegistrySettings)
    secrets: SecretsSettings = Field(default_factory=SecretsSettings)
    provisioner: ProvisionerSettings = Field(default_factory=ProvisionerSettings)
    pool: PoolSettings = Field(default_factory=PoolSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    ttl: TTLSettings = Field(default_factory=TTLSettings)
    reconciler: ReconcilerSettings = Field(default_factory=ReconcilerSettings)
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    billing: BillingSettings = Field(default_factory=BillingSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)

    model_config = SettingsConfigDict(
        env_prefix="KUBESANDBOX_",
        env_nested_delimiter="__",
        yaml_file=_YAML_FILE if _YAML_FILE.exists() else None,
        extra="ignore",
    )

    @model_validator(mode="after")
    def _guard_auth_disabled_outside_local(self) -> "Settings":
        if self.app_env != "local" and self.auth.disabled:
            raise ValueError("auth.disabled may only be true when app_env == 'local'")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority highest -> lowest: explicit init kwargs, then real env vars,
        # then the env-specific YAML file, then .env, then secret files.
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if _YAML_FILE.exists():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=_YAML_FILE))
        sources.extend([dotenv_settings, file_secret_settings])
        return tuple(sources)


@lru_cache
def get_settings() -> Settings:
    return Settings()
