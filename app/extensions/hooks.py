"""ComponentHook loader (doc §3.5) — the escape hatch for logic that can't be expressed
declaratively (e.g. creating a scoped Postgres role only once its sidecar is healthy).
Declarative-first; hooks only when needed — today that's exactly the DB components
under `components/databases/*/hooks.py`.

Only `on_provision`/`on_teardown` are wired to a real caller this phase
(SandboxService, after acquire()/before destroy()). `validate`/`mutate_pod_spec` are
part of the Protocol (doc §3.5's full illustrative contract) but have no driving code
yet — nothing in Phase 5's actual requirements needs them, and wiring them
speculatively would be exactly the "building for hypothetical future requirements"
this project avoids elsewhere. Left as a documented gap (see
docs/TASK_CHECKLIST.md's Phase 5 section), not silently dropped.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Protocol

from app.domain.execution import SandboxHandle
from app.domain.manifests import Component
from app.provisioners.base import Provisioner
from app.services.credentials import DbCredentials


@dataclass(frozen=True)
class RenderContext:
    """Everything a hook needs to act on its own sidecar: the component manifest, the
    credentials generate_db_credentials() already produced (and that are already
    baked into main's DATABASE_URL-style env var — see template_render.py), and the
    Provisioner to exec into the sidecar with. A bare SandboxHandle carries no
    behavior of its own, so the hook can't act on it without this.
    """

    component: Component
    credentials: DbCredentials
    provisioner: Provisioner


class ComponentHook(Protocol):
    async def validate(self, ctx: Any) -> None:
        """Publish-time manifest checks. Not wired to any caller yet — see module
        docstring."""
        ...

    async def mutate_pod_spec(self, spec: Any, ctx: RenderContext) -> Any:
        """Declarative spec mutation hook. Not wired to any caller yet — see module
        docstring."""
        ...

    async def on_provision(self, sb: SandboxHandle, ctx: RenderContext) -> None:
        """Runs once the sidecar container/pod is up and healthy — e.g. create the
        scoped DB role `ctx.credentials` already promised main's DATABASE_URL."""
        ...

    async def on_teardown(self, sb: SandboxHandle) -> None:
        """Runs before the sandbox is destroyed. A documented no-op for every hook in
        this codebase today: whole-sandbox teardown already wipes everything these
        hooks touch (the sidecar container/pod itself is destroyed right after)."""
        ...


def load_hook(module_path: str) -> ComponentHook:
    """`module_path` is a component manifest's `spec.hooks.module` (e.g.
    "components.databases.postgresql.hooks") — a plain dotted Python import path. The
    module itself is expected to expose a module-level `hook: ComponentHook`
    instance."""
    module = importlib.import_module(module_path)
    try:
        return module.hook
    except AttributeError as exc:
        raise ValueError(f"hook module {module_path!r} has no module-level `hook` instance") from exc
