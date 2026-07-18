"""TemplateService — SandboxTemplate listing/retrieval (entitlement-filtered) and
creation (doc §17: GET/POST /v1/templates). Same live-registry-mutation + disk-write
model as RegistryService — see its module docstring.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ComponentNotFoundError, EntitlementError, ManifestValidationError
from app.domain.auth import Principal
from app.domain.manifests import SandboxTemplate
from app.extensions.loader import (
    TEMPLATES_DIR,
    Registry,
    registry_key_for,
    validate_template_manifest,
)
from app.persistence.models import TemplateRecord
from app.services.entitlement_service import EntitlementService


class TemplateService:
    def __init__(
        self,
        registry: Registry,
        session: AsyncSession,
        entitlements: EntitlementService,
        templates_dir: Path = TEMPLATES_DIR,
    ) -> None:
        self._registry = registry
        self._session = session
        self._entitlements = entitlements
        self._templates_dir = templates_dir

    async def list_templates(
        self, principal: Principal, *, name: str | None = None
    ) -> list[tuple[str, SandboxTemplate]]:
        visible = await self._entitlements.filter_templates(
            self._registry.templates, principal, self._registry
        )
        items = visible.items()
        if name is not None:
            items = ((key, t) for key, t in items if key.rsplit("@", 1)[0] == name)
        return sorted(items, key=lambda kv: kv[0])

    async def create_template(self, raw: dict, principal: Principal) -> tuple[str, SandboxTemplate]:
        is_admin = self._entitlements.is_admin(principal)
        if not is_admin and not await self._entitlements.can_publish(principal, "template"):
            raise EntitlementError(
                "not entitled to publish templates — ask an admin for a 'template' "
                "publish grant (doc §3.6)"
            )

        name = raw.get("metadata", {}).get("name", "template")
        version = raw.get("metadata", {}).get("version", "0")
        target_dir = (
            self._templates_dir
            if is_admin
            else self._templates_dir / "tenant" / principal.tenant_id
        )
        # Versioned by directory, not just name — same reasoning as RegistryService:
        # multiple versions of one template can coexist, and must coexist on disk too.
        path = target_dir / name / f"{version}.yaml"

        template = validate_template_manifest(raw, source=path)

        refs = [template.spec.base.ref, *(c.ref for c in template.spec.components)]
        for ref in refs:
            try:
                self._registry.resolve_component_ref(ref)
            except ComponentNotFoundError as exc:
                raise ManifestValidationError(
                    f"template {template.key} has unresolved component ref {ref!r}"
                ) from exc
            if not is_admin and not await self._entitlements.is_ref_visible(
                ref, principal, self._registry
            ):
                raise EntitlementError(
                    f"template {template.key} references {ref!r}, which you are not "
                    "entitled to see — cannot publish a template depending on a "
                    "component you don't have visibility into"
                )

        key = registry_key_for(path, template.key, self._templates_dir)
        if key in self._registry.templates:
            raise ManifestValidationError(f"template {key!r} is already registered")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(raw, sort_keys=False))

        self._registry.templates[key] = template

        bare_name, _, version = key.rpartition("@")
        self._session.add(TemplateRecord(name=bare_name, version=version, manifest=raw))
        await self._session.flush()
        await self._session.commit()

        return key, template
