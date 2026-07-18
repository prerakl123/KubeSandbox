"""Generates a DB sidecar's scoped-role credentials (and a throwaway bootstrap admin
password) BEFORE a sandbox is acquired (doc §3.5/§16, roadmap Phase 5).

The tricky ordering this exists to solve: `DATABASE_URL` has to be baked into main's
env at container-creation time (env can't be added to an already-running
container/pod), but the scoped role it points at is only actually created afterward —
once the sidecar is healthy — by that component's on_provision hook. Generating both
passwords up front, here, and threading the SAME DbCredentials through both acquire()
(via template_render's env injection) and on_provision (via ComponentHook's
RenderContext) is what keeps those two promises in sync; see
template_render.render_template and app.extensions.hooks.RenderContext.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from app.domain.manifests import Component

_SANDBOX_DATABASE_NAME = "sandbox"
_DEFAULT_ROLE = "sandbox_user"
_PASSWORD_BYTES = 24


@dataclass(frozen=True)
class DbCredentials:
    """The scoped role a DB sidecar's on_provision hook creates, plus the throwaway
    password used to bootstrap/administer the sidecar itself (e.g. Postgres'
    POSTGRES_PASSWORD, MySQL's MYSQL_ROOT_PASSWORD) — never the same secret, so the
    sandboxed workload's own credential is never also the DB image's own admin secret.
    """

    role: str
    password: str
    database: str
    admin_password: str


def generate_db_credentials(component: Component) -> DbCredentials:
    access = component.spec.access.database
    role = access.role if access and access.role else _DEFAULT_ROLE
    return DbCredentials(
        role=role,
        password=secrets.token_urlsafe(_PASSWORD_BYTES),
        database=_SANDBOX_DATABASE_NAME,
        admin_password=secrets.token_urlsafe(_PASSWORD_BYTES),
    )
