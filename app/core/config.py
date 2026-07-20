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


class PoolSettings(BaseModel):
    """Warm-pool sizing per weight class (doc §4.3). Disabled by default in local."""

    enabled: bool = False
    light_pool_size: int = 0
    standard_pool_size: int = 0
    heavy_pool_size: int = 0


class WorkspaceSettings(BaseModel):
    """Persistent workspace quota/retention defaults (doc §10.2)."""

    persistence_enabled: bool = False
    default_quota_mb: int = 10 * 1024  # 10 GiB
    idle_retention_days: int = 30
    archive_grace_days: int = 60
    max_lifetime_days: int = 365


class ExecutionLimits(BaseModel):
    default_wall_clock_seconds: int = 60
    max_output_bytes: int = 5_000_000
    max_processes: int = 128


class BillingSettings(BaseModel):
    default_mode: Literal["credit", "payg"] = "credit"


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
