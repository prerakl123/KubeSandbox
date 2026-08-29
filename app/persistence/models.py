"""SQLAlchemy 2.0 ORM models for the control-plane database (doc §10.1).

Tables backing the Phase 0/1 vertical slice (tenants, users, api_keys, components,
templates, sandboxes, runs, audit_logs) are load-bearing today, as are entitlements
(Phase 2) and pooling/workspaces (Phase 7, as of this revision). The billing tables
remain schema-only, laid down now so Phase 8 doesn't need a disruptive migration — no
service code writes to them yet.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


# --- Core tenancy & auth -----------------------------------------------------------

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    role: Mapped[str] = mapped_column(String(32), default="user")  # admin | operator | user
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """First few plaintext characters of the key, stored in the clear for display
    (Phase 9's `POST/GET /v1/api-keys`). The full key is unrecoverable — only its hash
    is kept (doc §11) — so a UI needs *something* to render in a list, and a caller
    needs something to match against the key they saved. Deliberately short enough to
    leak nothing useful against 256 bits of entropy. Nullable because keys created
    before this column existed (by direct DB insert, the only way there was) have none."""
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    """Who minted it. Nullable: a key minted by another *key* (role `service`) has no
    user to attribute to, and neither do pre-Phase-9 rows."""
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Updated by the auth path on use. The one piece of information that makes
    "is this key still needed?" answerable — without it, revoking safely means guessing.
    Written on a best-effort basis; see `app/api/deps.py::_principal_from_api_key`."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Registry projection (source of truth is git; this is an indexed cache) --------

class ComponentRecord(Base):
    __tablename__ = "components"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_component_name_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))
    manifest: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TemplateRecord(Base):
    __tablename__ = "templates"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_template_name_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Catalog curation (doc §3.6) — schema only until Phase 2 wires enforcement -----

class ComponentEntitlement(Base):
    __tablename__ = "component_entitlements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope: Mapped[str] = mapped_column(String(16))  # tenant | user
    scope_id: Mapped[str] = mapped_column(String(36))
    component_name: Mapped[str] = mapped_column(String(128))
    version_range: Mapped[str] = mapped_column(String(64), default="*")
    visible: Mapped[bool] = mapped_column(Boolean, default=True)


class PublishGrant(Base):
    __tablename__ = "publish_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope: Mapped[str] = mapped_column(String(16))  # tenant | user
    scope_id: Mapped[str] = mapped_column(String(36))
    category: Mapped[str] = mapped_column(String(32))
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)


# --- Sandboxes & runs (Phase 0/1 load-bearing) --------------------------------------

class Sandbox(Base):
    __tablename__ = "sandboxes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    template_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    component_refs: Mapped[list] = mapped_column(JSON, default=list)
    backend: Mapped[str] = mapped_column(String(16))  # docker | kubernetes
    native_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sidecar_refs: Mapped[dict] = mapped_column(JSON, default=dict)
    """Sidecar component name -> provisioner-native ref (doc §20 Phase 5) — must be
    persisted, not just held on the in-memory SandboxHandle returned by acquire():
    destroy_sandbox() runs in a separate request and needs this to reconstruct which
    sidecar containers to tear down (Docker's sidecar container ids are opaque and
    otherwise unrecoverable; Kubernetes' happen to equal the name, but the column
    stays backend-agnostic either way)."""
    state: Mapped[str] = mapped_column(String(16), default="pending")
    weight_class: Mapped[str] = mapped_column(String(16), default="light")
    persistent: Mapped[bool] = mapped_column(Boolean, default=False)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), nullable=True)
    """Set only when `persistent` is true (Phase 7) — which Workspace's durable
    volume/PVC this sandbox mounted at `/workspace`. Null for every ephemeral sandbox,
    which is all of them before this phase."""
    idle_ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Reconciler TTL inputs (doc §4.1, Phase 7) — resolved once at create time (from
    the SandboxTemplate's `spec.ttl`, or `TTLSettings` defaults for an ad-hoc sandbox)
    and persisted here since the reconciler runs in a separate process/request from
    creation and has nothing else to read them from. Null means "never reap by TTL" —
    reserved for a future admin override, nothing sets it today."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    sandbox_id: Mapped[str | None] = mapped_column(ForeignKey("sandboxes.id"), nullable=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    command: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="completed")
    """pending | running | completed | failed — doc §5.1's `?async=true` poll contract
    ("poll GET /v1/runs/{run_id} until status=completed").

    Defaults to `completed` because every synchronous run writes its row only *after*
    finishing, which is every run before Phase 9: a `runs` row has always meant "a run
    that happened". Only the async path ever persists a row in a non-terminal state.
    `failed` means the run never produced a result at all (provisioning blew up, the
    control plane restarted mid-run) — a program exiting non-zero is a perfectly
    `completed` run with a non-zero `exit_code`."""
    component_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Which component ("name@version") actually ran. Nullable for rows written before
    this column existed; recorded now so a UI's run history can show the language
    without re-resolving the sandbox's component list."""
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why a `failed` run produced no result. Distinct from `stderr_excerpt`, which is
    the user program's own output — this is the control plane's own failure."""
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    variables: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When the run reached a terminal status. Null while pending/running. Only the
    async path leaves a visible gap between `created_at` and this."""


# --- Build system (doc §8, Phase 6) -------------------------------------------------

class Build(Base):
    __tablename__ = "builds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    component_name: Mapped[str] = mapped_column(String(128))
    component_version: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    """Null for a build of a public component; set for a tenant-private one — mirrors
    the same public/private split component_entitlements uses (doc §3.6)."""
    strategy: Mapped[str] = mapped_column(String(16))  # dockerfile | compose | pipeline | helm
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | running | succeeded | failed
    image_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Resolved, pullable ref after a successful image-kind Artifact is pushed via the
    configured ImageRegistryProvider — this is what populates Registry.built_images."""
    artifact_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Object-store key for a successful manifest-kind Artifact (HelmChartStrategy) —
    mutually exclusive with image_ref in practice, never both set."""
    log_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Persistent workspaces (doc §10.2) — schema only until Phase 7 -----------------

class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    quota_mb: Mapped[int] = mapped_column(Integer, default=10 * 1024)
    used_mb: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(16), default="active")  # active | archived | deleted
    last_access_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Pod pooling (doc §4.3) ----------------------------------------------------------

class PoolState(Base):
    """One aggregate row per (image_digest, weight_class) key — an observability
    counter (doc §14's `pool_hit_rate` metric needs a denominator), not the claim
    ledger itself; PoolMember below is the source of truth for which specific
    sandboxes are actually idle and claimable."""

    __tablename__ = "pool_state"
    __table_args__ = (UniqueConstraint("image_digest", "weight_class", name="uq_pool_state_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    image_digest: Mapped[str] = mapped_column(String(128))
    weight_class: Mapped[str] = mapped_column(String(16))
    idle_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Quota(Base):
    """Per-tenant resource ceilings (doc §10.1's `quotas` table, doc §11's "quotas (max
    concurrent sandboxes, cpu/mem, monthly minutes, credit balance/spend cap) enforced by
    QuotaService/BillingService before create").

    Doc §10.1 lists this table and it had never actually been created — the whole quota
    half of that sentence was unimplemented, with only billing pre-authorization gating
    creation. Added in the post-Phase-9 cross-cutting pass.

    One row per tenant, created lazily on first check with the configured defaults (the
    same `get_or_create` shape `BillingAccount` and `Workspace` use). A null column means
    "no limit for this dimension", which is what lets an operator cap concurrency without
    also having to invent a monthly-minutes number.

    Credit balance and spend cap deliberately live on `billing_accounts`/`credit_wallets`
    instead: doc §11 groups them with quotas conceptually, but they're enforced by a
    different service with different semantics (a wallet is consumed, a quota is a
    ceiling), and duplicating them here would create two sources of truth.
    """

    __tablename__ = "quotas"

    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    max_concurrent_sandboxes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Counted as non-terminated `sandboxes` rows. The single most important limit here:
    without it one tenant can occupy the whole cluster, and doc §4.3's weight-class
    segregation protects *classes* of workload from each other, not tenants."""
    max_cpu_millicores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Summed across the tenant's live sandboxes from each one's resolved *limits*, plus
    the pending request. Same "configured limits, not measured consumption" caveat Phase
    8's billing carries — there is no metrics pipeline feeding real usage back in."""
    max_monthly_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Wall-clock sandbox minutes per calendar month, derived from `runs.duration_ms` plus
    non-ephemeral sandbox lifetimes. The calendar month matches PAYG billing's own cycle
    definition so a tenant isn't reasoning about two different month boundaries."""
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PoolMember(Base):
    """One idle, claimable sandbox sitting in a warm pool (doc §4.3), keyed by
    `(image_ref, weight_class)`. Deliberately NOT a row in `sandboxes`: a pool member
    belongs to no tenant yet (`sandboxes.tenant_id` is required, on purpose — every
    *claimed* sandbox is still tenant-scoped from the moment it's claimed), and
    keeping the two tables separate means claiming one is a plain delete-and-recreate
    transfer instead of a tenant-reassignment update on a table whose FK doesn't allow
    a "no tenant yet" state. `PoolManager.try_claim()` is the only writer that deletes
    a row here (atomically, via `SELECT ... FOR UPDATE SKIP LOCKED`); `release()` is
    the only writer that inserts one."""

    __tablename__ = "pool_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    image_ref: Mapped[str] = mapped_column(String(255))
    weight_class: Mapped[str] = mapped_column(String(16))
    backend: Mapped[str] = mapped_column(String(16))
    native_ref: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Billing & costing (doc §13) — schema only until Phase 8 -----------------------

class BillingAccount(Base):
    __tablename__ = "billing_accounts"

    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), default="credit")  # credit | payg
    spend_cap: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")


class PricingRule(Base):
    __tablename__ = "pricing_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    resource_type: Mapped[str] = mapped_column(String(32))  # cpu_second | memory_gb_second | storage_gb_day | db_hour
    unit_cost: Mapped[float] = mapped_column(Numeric(12, 6))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    sandbox_id: Mapped[str | None] = mapped_column(ForeignKey("sandboxes.id"), nullable=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[float] = mapped_column(Numeric(14, 6))
    cost: Mapped[float] = mapped_column(Numeric(12, 6))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CreditWallet(Base):
    __tablename__ = "credit_wallets"

    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    balance: Mapped[float] = mapped_column(Numeric(14, 6), default=0)
    low_balance_threshold: Mapped[float] = mapped_column(Numeric(14, 6), default=0)


class CreditLedgerEntry(Base):
    __tablename__ = "credit_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    delta: Mapped[float] = mapped_column(Numeric(14, 6))
    reason: Mapped[str] = mapped_column(String(255))
    balance_after: Mapped[float] = mapped_column(Numeric(14, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    total_cost: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | pending_payment | paid
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)  # future payment-gateway id


class CreditRequest(Base):
    """A tenant's self-service ask for more credit (credit mode) or spend-cap
    headroom (PAYG mode) — not in doc §13's original design, added on top of it so a
    tenant that hits `BillingAuthorizationError` has a real path forward besides
    asking an admin out of band. Approving one applies the amount immediately via
    `BillingService.adjust_credit()`/`set_mode()` — see that service for the mode
    dispatch."""

    __tablename__ = "credit_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 6))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | denied
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --- Audit ---------------------------------------------------------------------------

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
