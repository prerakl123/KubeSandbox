"""SQLAlchemy 2.0 ORM models for the control-plane database (doc §10.1).

Tables backing the Phase 0/1 vertical slice (tenants, users, api_keys, components,
templates, sandboxes, runs, audit_logs) are load-bearing today. The entitlement,
workspace, pool, and billing tables are schema laid down now so later phases (§20:
Phase 2 entitlements, Phase 7 pooling/persistence, Phase 8 billing) don't need a
disruptive migration — no service code writes to them yet.
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
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    sandbox_id: Mapped[str | None] = mapped_column(ForeignKey("sandboxes.id"), nullable=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    command: Mapped[list] = mapped_column(JSON, default=list)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    variables: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
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


# --- Pod pooling (doc §4.3) — schema only until Phase 7 -----------------------------

class PoolState(Base):
    __tablename__ = "pool_state"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    image_digest: Mapped[str] = mapped_column(String(128))
    weight_class: Mapped[str] = mapped_column(String(16))
    idle_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
