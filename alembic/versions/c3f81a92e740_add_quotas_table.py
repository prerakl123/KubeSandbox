"""add quotas table

Revision ID: c3f81a92e740
Revises: 9a1c4e77b210
Create Date: 2026-08-28 21:40:00.000000

Doc §10.1 lists `quotas` among the core tables and it had never been created — the quota
half of doc §11's "quotas ... enforced by QuotaService/BillingService before create" was
entirely unimplemented, leaving nothing but billing to bound a tenant (and billing is off
by default in both env profiles, so in practice nothing at all).

One row per tenant, `tenant_id` as the primary key rather than a surrogate id: a tenant
has exactly one quota, so a separate id plus a unique constraint would only add a way for
two rows to disagree. Same shape `billing_accounts` and `credit_wallets` already use.

Every ceiling is nullable, and null means "no limit" — that is what lets an operator cap
concurrency without also having to invent a monthly-minutes number. Rows materialize
lazily from `QuotaSettings` defaults on first check, so this migration deliberately
inserts nothing.

Hand-written, like `9a1c4e77b210` before it: this session still had no reachable Postgres
(see the checklist's Docker root-cause note — the host's ext4 journal is wedged). A single
`create_table`/`drop_table` pair, which is exactly what autogenerate would emit for a new
model. **Not yet applied to a real database.**
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f81a92e740"
down_revision: Union[str, Sequence[str], None] = "9a1c4e77b210"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "quotas",
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("max_concurrent_sandboxes", sa.Integer(), nullable=True),
        sa.Column("max_cpu_millicores", sa.Integer(), nullable=True),
        sa.Column("max_memory_mb", sa.Integer(), nullable=True),
        sa.Column("max_monthly_minutes", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("tenant_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("quotas")
