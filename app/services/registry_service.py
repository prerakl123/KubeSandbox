"""RegistryService — component listing/retrieval (entitlement-filtered) and
registration (doc §17: GET/POST /v1/components).

Registration mutates the live in-memory Registry directly (no reload-the-world step)
and also writes the manifest to disk under components/ so it survives a restart — the
same "git-backed source of truth, DB is an indexed projection" model the disk loader
already uses (doc §3.5), just reached via an API call instead of a commit. A
tenant-private registration (doc §3.6) lands under components/tenant/<tenant_id>/<name>/
instead, gated by a publish_grant rather than admin status.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ComponentNotFoundError, EntitlementError, ManifestValidationError
from app.domain.auth import Principal
from app.domain.manifests import Component
from app.extensions.loader import (
    COMPONENTS_DIR,
    Registry,
    registry_key_for,
    validate_component_manifest,
    version_sort_key,
)
from app.persistence.models import ComponentRecord
from app.services.entitlement_service import EntitlementService

_CATEGORY_DIRS = {
    "language": "languages",
    "database": "databases",
    "tool": "tools",
    "service": "services",
    "base": "base",
    "build-strategy": "build-strategies",
}


class RegistryService:
    def __init__(
        self,
        registry: Registry,
        session: AsyncSession,
        entitlements: EntitlementService,
        components_dir: Path = COMPONENTS_DIR,
    ) -> None:
        self._registry = registry
        self._session = session
        self._entitlements = entitlements
        self._components_dir = components_dir

    async def list_components(
        self, principal: Principal, *, category: str | None = None
    ) -> list[tuple[str, Component]]:
        visible = await self._entitlements.filter_components(self._registry.components, principal)
        items = visible.items()
        if category is not None:
            items = ((key, c) for key, c in items if c.metadata.category == category)
        return sorted(items, key=lambda kv: kv[0])

    async def get_component_versions(
        self, name: str, principal: Principal
    ) -> list[tuple[str, Component]]:
        """All versions of a bare or tenant-qualified component name visible to the
        caller, newest first."""
        visible = await self._entitlements.filter_components(self._registry.components, principal)
        matches = [(key, c) for key, c in visible.items() if key.rsplit("@", 1)[0] == name]
        if not matches:
            raise ComponentNotFoundError(name)
        matches.sort(key=lambda kv: version_sort_key(kv[1].metadata.version), reverse=True)
        return matches

    async def register_component(self, raw: dict, principal: Principal) -> tuple[str, Component]:
        is_admin = self._entitlements.is_admin(principal)
        category = raw.get("metadata", {}).get("category")

        if not is_admin:
            if category is None or not await self._entitlements.can_publish(principal, category):
                raise EntitlementError(
                    f"not entitled to publish {category or 'this'} components — ask an "
                    "admin for a publish grant (doc §3.6)"
                )

        name = raw.get("metadata", {}).get("name", "component")
        version = raw.get("metadata", {}).get("version", "0")
        target_dir = (
            self._components_dir / _CATEGORY_DIRS.get(category, category or "misc")
            if is_admin
            else self._components_dir / "tenant" / principal.tenant_id
        )
        # Versioned by directory, not just name: multiple versions of one component
        # coexist in the registry (doc §3.5) and must coexist on disk too, or
        # registering a new version clobbers the previous version's file.
        path = target_dir / name / version / "component.yaml"

        # Validate before touching disk or the live registry — a bad manifest must
        # never partially land.
        component = validate_component_manifest(raw, source=path)

        for req in component.spec.requires:
            try:
                self._registry.resolve_component_ref(req)
            except ComponentNotFoundError as exc:
                raise ManifestValidationError(
                    f"component {component.key} requires unresolved dependency {req!r}"
                ) from exc
            if not is_admin and not await self._entitlements.is_ref_visible(
                req, principal, self._registry
            ):
                raise EntitlementError(
                    f"component {component.key} requires {req!r}, which you are not "
                    "entitled to see — cannot publish a component depending on a "
                    "component you don't have visibility into"
                )

        key = registry_key_for(path, component.key, self._components_dir)
        if key in self._registry.components:
            raise ManifestValidationError(f"component {key!r} is already registered")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(raw, sort_keys=False))

        self._registry.components[key] = component
        self._registry.component_dirs[key] = path.parent

        bare_name, _, version = key.rpartition("@")
        self._session.add(
            ComponentRecord(
                name=bare_name,
                version=version,
                category=component.metadata.category,
                manifest=raw,
            )
        )
        await self._session.flush()
        await self._session.commit()

        return key, component
