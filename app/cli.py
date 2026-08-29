"""Operator CLI — one-time setup a running API can't perform on itself.

Only `seed-admin` for now, and it exists for the same chicken-and-egg
`auth.bootstrap_admin_emails` solves, from the other direction: the allowlist promotes on
*OIDC login*, so it does nothing in an environment with no IdP at all (`local`, a kind
cluster, a fresh prod database before the AAD app registration exists). This creates the
tenant and user directly.

Deliberately a separate entrypoint rather than an endpoint. Running it requires shell
access to a pod or machine that already holds the database credential — which is exactly
the privilege level that *should* be required to mint the first admin, and is a much
better boundary than an HTTP route protected by a shared bootstrap token that then has to
be rotated or disabled forever after.

    uv run python -m app.cli seed-admin --email you@example.com
    uv run python -m app.cli seed-admin --email you@example.com --tenant acme
    uv run python -m app.cli list-admins
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.persistence.db import get_session_factory
from app.persistence.models import Tenant, User

_DEFAULT_TENANT_NAME = "bootstrap"


async def seed_admin(email: str, tenant_name: str, *, promote_existing: bool) -> int:
    """Create (or promote) an admin user, and its tenant if needed.

    Idempotent by design: re-running with the same arguments reports the existing state
    rather than failing, so it's safe in a provisioning script that may run twice.
    """
    async with get_session_factory()() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.name == tenant_name))
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(name=tenant_name)
            session.add(tenant)
            await session.flush()
            print(f"created tenant {tenant_name!r} ({tenant.id})")
        else:
            print(f"using existing tenant {tenant_name!r} ({tenant.id})")

        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(tenant_id=tenant.id, email=email, role="admin")
            session.add(user)
            await session.flush()
            await session.commit()
            print(f"created admin {email} ({user.id}) in tenant {tenant.id}")
            return 0

        if user.role == "admin":
            print(f"{email} is already an admin ({user.id}) — nothing to do")
            return 0

        if not promote_existing:
            # Refused rather than silently promoted: this account may belong to a
            # different tenant than the one named, and quietly changing a real user's
            # role is not something a "seed" command should do by default.
            print(
                f"{email} already exists with role {user.role!r} in tenant {user.tenant_id}. "
                "Re-run with --promote-existing to promote them.",
                file=sys.stderr,
            )
            return 1

        previous = user.role
        user.role = "admin"
        await session.commit()
        print(f"promoted {email} ({user.id}) from {previous!r} to 'admin'")
        return 0


async def list_admins() -> int:
    """Answers "is there an admin at all, and who?" — the question you have when a UI is
    returning 403 and you don't know whether bootstrap ever happened."""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(User, Tenant)
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(User.role == "admin")
                .order_by(User.created_at)
            )
        ).all()
    if not rows:
        print("no admins exist — run `seed-admin` or set auth.bootstrap_admin_emails")
        return 1
    for user, tenant in rows:
        print(f"{user.email}\t{user.id}\ttenant={tenant.name} ({tenant.id})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed = subparsers.add_parser(
        "seed-admin",
        help="Create or promote an admin user (idempotent).",
        description="Creates the tenant if it doesn't exist. Safe to re-run.",
    )
    seed.add_argument("--email", required=True, help="Admin's email address.")
    seed.add_argument(
        "--tenant",
        default=_DEFAULT_TENANT_NAME,
        help=f"Tenant name to create/use (default: {_DEFAULT_TENANT_NAME!r}).",
    )
    seed.add_argument(
        "--promote-existing",
        action="store_true",
        help="Promote the account if it already exists with a non-admin role.",
    )

    subparsers.add_parser("list-admins", help="List every admin user and its tenant.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(debug=settings.debug)

    if args.command == "seed-admin":
        return asyncio.run(
            seed_admin(args.email, args.tenant, promote_existing=args.promote_existing)
        )
    if args.command == "list-admins":
        return asyncio.run(list_admins())
    raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
