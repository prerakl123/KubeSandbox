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
    # "aws"/"gcp" are selectable for the same reason ObjectStorageSettings above lists
    # them — so a deployment pointed at an unimplemented cloud fails loudly at startup
    # (doc §9) instead of at the first secret lookup.
    provider: Literal["dotenv", "azure_keyvault", "aws", "gcp"] = "dotenv"
    vault_url: str | None = None

    @model_validator(mode="after")
    def _keyvault_needs_a_vault_url(self) -> "SecretsSettings":
        # AzureKeyVaultSecretsProvider can't be constructed meaningfully without one,
        # and catching it here (while parsing Settings) is strictly earlier than
        # catching it in the provider's own __init__.
        if self.provider == "azure_keyvault" and not self.vault_url:
            raise ValueError("secrets.vault_url is required when secrets.provider == 'azure_keyvault'")
        return self


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


class ObservabilitySettings(BaseModel):
    """Prometheus metrics + OpenTelemetry tracing (doc §14, §20 Phase 9).

    Metrics default to *on* everywhere: `prometheus_client` collects in-process with no
    external dependency, `GET /metrics` is a plain scrape endpoint, and a control plane
    that can't report `sandboxes_active`/`provision_latency` is exactly what doc §14
    says it must be able to report. Tracing defaults to *off* because it's the opposite
    shape — an OTLP exporter needs a real collector endpoint to ship spans to, and
    pointing it at a non-existent one just produces a background retry loop and noise.
    """

    metrics_enabled: bool = True
    tracing_enabled: bool = False
    otlp_endpoint: str | None = None
    """gRPC OTLP collector address (e.g. "http://otel-collector:4317"). Required when
    `tracing_enabled` — validated below rather than defaulted, since a silently-wrong
    default endpoint is worse than a startup failure."""
    service_name: str = "kubesandbox"
    """Reported as OTel's `service.name` resource attribute; the reconciler worker
    overrides it to "kubesandbox-reconciler" so its spans are separable from the API's."""
    trace_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    """Parent-based ratio sampler. 1.0 (all spans) is right for `local` and for
    aks-prod's initial rollout; lower it once span volume becomes a cost concern."""

    @model_validator(mode="after")
    def _tracing_needs_an_endpoint(self) -> "ObservabilitySettings":
        if self.tracing_enabled and not self.otlp_endpoint:
            raise ValueError(
                "observability.otlp_endpoint is required when observability.tracing_enabled is true"
            )
        return self


class CorsSettings(BaseModel):
    """Browser access for the standalone-user UI (doc §1's second consumer).

    A cross-origin frontend cannot make a single call without this — not a nice-to-have
    but the first hard blocker any UI hits. Deliberately an explicit allowlist with no
    wildcard default: `allow_credentials=True` plus `allow_origins=["*"]` is rejected
    by every browser anyway, and a permissive default in a service that hands out
    sandbox sessions is the wrong way round.
    """

    enabled: bool = False
    allow_origins: list[str] = Field(default_factory=list)
    """Exact origins, scheme included (e.g. "https://kubesandbox.example.com"). No
    trailing slash — browsers send the origin without one and a mismatch fails silently
    from the caller's point of view."""
    allow_credentials: bool = True
    """Needed only if the UI ever relies on cookies. The auth design here is
    `Authorization: Bearer`, which does not, but leaving this on costs nothing and
    keeps a cookie-based variant open."""
    allow_methods: list[str] = Field(default_factory=lambda: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    allow_headers: list[str] = Field(default_factory=lambda: ["Authorization", "Content-Type", "X-API-Key"])
    expose_headers: list[str] = Field(default_factory=list)
    max_age: int = 600

    @model_validator(mode="after")
    def _enabled_needs_origins(self) -> "CorsSettings":
        if self.enabled and not self.allow_origins:
            raise ValueError("cors.allow_origins must list at least one origin when cors.enabled is true")
        if "*" in self.allow_origins and self.allow_credentials:
            # Browsers reject this combination outright; failing here makes that
            # obvious at startup instead of as an unexplained CORS error in a console.
            raise ValueError("cors.allow_origins cannot contain '*' while cors.allow_credentials is true")
        return self


class AuthSettings(BaseModel):
    """Two authentication paths coexist (doc §11), and a caller may use either:

    * **Service accounts / the workflow-builder** — a hashed API key in `X-API-Key`.
      Long-lived, minted per tenant (`POST /v1/api-keys`).
    * **Standalone human users / the UI** — OIDC (Azure AD) exchanged once for a
      short-lived KubeSandbox session JWT, then sent as `Authorization: Bearer`. The
      exchange is `POST /v1/auth/token`; see `app/services/auth_service.py` for why the
      session token is issued locally rather than the IdP's own token being validated
      on every request.
    """

    # Convenience-only escape hatch for local dev so /execute is reachable without
    # standing up an IdP first. Guarded below: forbidden outside "local".
    disabled: bool = False
    jwt_secret: str = "change-me-in-local-dev-only"
    """HS256 signing key for KubeSandbox's *own* session tokens (never for validating
    the IdP's — those are RS256, verified against the issuer's JWKS). Must come from
    Key Vault / a K8s Secret in `aks-prod`; the default here is a local-dev placeholder
    and the validator below refuses it outside `local`."""
    session_ttl_seconds: int = 3_600
    """Doc §11's "short-lived JWT session". One hour: long enough that a UI isn't
    re-exchanging constantly, short enough that a leaked token expires on its own. A UI
    re-runs the OIDC exchange to renew — there is deliberately no refresh-token flow,
    since the IdP already holds the long-lived session."""
    oidc_issuer: str | None = None
    """e.g. "https://login.microsoftonline.com/<aad-tenant>/v2.0". Discovery
    (`/.well-known/openid-configuration`) resolves the JWKS URI from this."""
    oidc_audience: str | None = None
    """The app registration's client id. Validated as the token's `aud` — without this
    check, a token minted for *any* other application in the same AAD tenant would be
    accepted here."""
    oidc_client_id: str | None = None
    """Published to the UI by `GET /v1/auth/config` so the frontend's MSAL setup isn't
    hardcoded per environment. Usually the same value as `oidc_audience`; kept separate
    because they legitimately differ when the API is a distinct app registration from
    the SPA."""
    oidc_jwks_url: str | None = None
    """Optional explicit override, skipping OIDC discovery. Useful for an IdP with a
    non-standard discovery document, or to avoid one network round trip at first use."""
    oidc_tenant_claim: str = "tid"
    """Which claim identifies the caller's tenant. `tid` (the AAD directory id) is the
    right default; a deployment that maps several KubeSandbox tenants onto one AAD
    directory should point this at a custom claim emitted by the IdP instead."""
    oidc_email_claim: str = "preferred_username"
    """AAD v2.0 puts the sign-in name here; `email` is only present when the optional
    claim is configured on the app registration. `AuthService` falls back through
    `email` and `sub` if this claim is absent, so a misconfigured app registration
    degrades to a stable-but-ugly identity rather than a hard failure."""


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
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)

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

    @model_validator(mode="after")
    def _guard_placeholder_jwt_secret_outside_local(self) -> "Settings":
        # The committed default is a placeholder; shipping it to prod would let anyone
        # who has read this repo mint a valid admin session token. Same class of guard
        # as `auth.disabled` above, and for the same reason: a config mistake here is
        # a full authentication bypass, so it must be impossible to deploy quietly.
        if self.app_env == "local":
            return self
        if self.auth.jwt_secret == AuthSettings.model_fields["jwt_secret"].default:
            raise ValueError(
                "auth.jwt_secret must be overridden outside app_env='local' "
                "(inject it from Key Vault / a Kubernetes Secret)"
            )
        # RFC 7518 §3.2's floor for HS256, which PyJWT itself warns about below 32
        # bytes: an HMAC key shorter than the digest is brute-forcible offline, and
        # this key is what mints session tokens.
        if len(self.auth.jwt_secret.encode()) < 32:
            raise ValueError("auth.jwt_secret must be at least 32 bytes (RFC 7518 §3.2 for HS256)")
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
