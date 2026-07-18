"""Catalog curation (doc §3.6): admins see/manage the *entire* registry; everyone else
sees only what an admin has explicitly entitled them to, plus their own private,
tenant-namespaced components/templates regardless of any entitlement row — and never
another tenant's private ones, regardless either. Admin endpoints bypass all of this.

Ownership of a private component/template isn't a separate DB column — it's derived
structurally from its registry key ("tenant/<tenant_id>/<name>@<version>", see
app/extensions/loader.py's registry_key_for), so there's nothing to keep in sync.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ComponentNotFoundError
from app.domain.auth import Principal
from app.domain.manifests import Component, SandboxTemplate
from app.extensions.loader import Registry
from app.persistence.models import ComponentEntitlement, PublishGrant


def _private_owner(qualified_name: str) -> str | None:
    """`qualified_name` is either a full registry key ('tenant/<id>/<name>@<version>')
    or just its name-portion ('tenant/<id>/<name>') — both split the same way. Returns
    the owning tenant id, or None if this isn't a tenant-private qualified name."""
    parts = qualified_name.split("/", 2)
    if len(parts) == 3 and parts[0] == "tenant":
        return parts[1]
    return None


def _version_in_range(version: str, version_range: str) -> bool:
    """Deliberately simple: the doc doesn't specify a full semver-range grammar for
    component_entitlements.version_range, so "*" matches any version and anything else
    must match the component's version exactly."""
    return version_range == "*" or version_range == version


def _is_entitled(
    component: Component, principal: Principal, entitlements: Sequence[ComponentEntitlement]
) -> bool:
    return any(
        e.visible
        and e.component_name == component.metadata.name
        and _version_in_range(component.metadata.version, e.version_range)
        and (
            (e.scope == "tenant" and e.scope_id == principal.tenant_id)
            or (e.scope == "user" and e.scope_id == principal.user_id)
        )
        for e in entitlements
    )


class EntitlementService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def is_admin(principal: Principal) -> bool:
        return principal.role == "admin"

    async def _load_entitlements_for(self, principal: Principal) -> list[ComponentEntitlement]:
        scope_ids = [principal.tenant_id, *([principal.user_id] if principal.user_id else [])]
        rows = (
            await self._session.execute(
                select(ComponentEntitlement).where(ComponentEntitlement.scope_id.in_(scope_ids))
            )
        ).scalars().all()
        return list(rows)

    def can_see_component(
        self,
        key: str,
        component: Component,
        principal: Principal,
        entitlements: Sequence[ComponentEntitlement],
    ) -> bool:
        if self.is_admin(principal):
            return True
        owner_tenant_id = _private_owner(key)
        if owner_tenant_id is not None:
            return owner_tenant_id == principal.tenant_id
        return _is_entitled(component, principal, entitlements)

    async def filter_components(
        self, items: dict[str, Component], principal: Principal
    ) -> dict[str, Component]:
        if self.is_admin(principal):
            return dict(items)
        entitlements = await self._load_entitlements_for(principal)
        return {
            key: component
            for key, component in items.items()
            if self.can_see_component(key, component, principal, entitlements)
        }

    def _ref_visible(
        self,
        ref: str,
        principal: Principal,
        entitlements: Sequence[ComponentEntitlement],
        registry: Registry,
    ) -> bool:
        owner_tenant_id = _private_owner(ref.rpartition("@")[0])
        if owner_tenant_id is not None:
            return owner_tenant_id == principal.tenant_id
        try:
            component = registry.resolve_component_ref(ref)
        except ComponentNotFoundError:
            return False
        return _is_entitled(component, principal, entitlements)

    async def is_ref_visible(self, ref: str, principal: Principal, registry: Registry) -> bool:
        """Public single-ref visibility check — used when a component/template being
        *published* references another component (`requires`, a template's `base`/
        `components`), so a non-admin can't smuggle in a dependency on something they
        can't otherwise see (e.g. guessing another tenant's private component ref)."""
        if self.is_admin(principal):
            return True
        entitlements = await self._load_entitlements_for(principal)
        return self._ref_visible(ref, principal, entitlements, registry)

    async def filter_templates(
        self,
        templates: dict[str, SandboxTemplate],
        principal: Principal,
        registry: Registry,
    ) -> dict[str, SandboxTemplate]:
        """A public template is visible only if *every* component it references (base +
        components) is itself visible to the caller — there's no separate
        template_entitlements table (doc §3.6 only defines one for components), so
        template visibility is derived from its component refs rather than tracked
        independently."""
        if self.is_admin(principal):
            return dict(templates)
        entitlements = await self._load_entitlements_for(principal)
        result: dict[str, SandboxTemplate] = {}
        for key, template in templates.items():
            owner_tenant_id = _private_owner(key)
            if owner_tenant_id is not None:
                if owner_tenant_id == principal.tenant_id:
                    result[key] = template
                continue
            refs = [template.spec.base.ref, *(c.ref for c in template.spec.components)]
            if all(self._ref_visible(ref, principal, entitlements, registry) for ref in refs):
                result[key] = template
        return result

    # -- publish grants (doc §3.6: may a scope publish its own private catalog entries?) --

    async def can_publish(self, principal: Principal, category: str) -> bool:
        if self.is_admin(principal):
            return True
        scope_ids = [principal.tenant_id, *([principal.user_id] if principal.user_id else [])]
        rows = (
            await self._session.execute(
                select(PublishGrant).where(
                    PublishGrant.category == category,
                    PublishGrant.allowed.is_(True),
                    PublishGrant.scope_id.in_(scope_ids),
                )
            )
        ).scalars().all()
        return len(rows) > 0

    # -- admin management: GET/PATCH /v1/admin/entitlements & /v1/admin/publish-grants --

    async def list_entitlements(
        self,
        *,
        scope: str | None = None,
        scope_id: str | None = None,
        component_name: str | None = None,
    ) -> list[ComponentEntitlement]:
        stmt = select(ComponentEntitlement)
        if scope is not None:
            stmt = stmt.where(ComponentEntitlement.scope == scope)
        if scope_id is not None:
            stmt = stmt.where(ComponentEntitlement.scope_id == scope_id)
        if component_name is not None:
            stmt = stmt.where(ComponentEntitlement.component_name == component_name)
        return list((await self._session.execute(stmt)).scalars().all())

    async def upsert_entitlement(
        self,
        *,
        scope: str,
        scope_id: str,
        component_name: str,
        version_range: str = "*",
        visible: bool = True,
    ) -> ComponentEntitlement:
        existing = (
            await self._session.execute(
                select(ComponentEntitlement).where(
                    ComponentEntitlement.scope == scope,
                    ComponentEntitlement.scope_id == scope_id,
                    ComponentEntitlement.component_name == component_name,
                    ComponentEntitlement.version_range == version_range,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.visible = visible
            row = existing
        else:
            row = ComponentEntitlement(
                scope=scope,
                scope_id=scope_id,
                component_name=component_name,
                version_range=version_range,
                visible=visible,
            )
            self._session.add(row)
        await self._session.flush()
        await self._session.commit()
        return row

    async def list_publish_grants(
        self,
        *,
        scope: str | None = None,
        scope_id: str | None = None,
        category: str | None = None,
    ) -> list[PublishGrant]:
        stmt = select(PublishGrant)
        if scope is not None:
            stmt = stmt.where(PublishGrant.scope == scope)
        if scope_id is not None:
            stmt = stmt.where(PublishGrant.scope_id == scope_id)
        if category is not None:
            stmt = stmt.where(PublishGrant.category == category)
        return list((await self._session.execute(stmt)).scalars().all())

    async def upsert_publish_grant(
        self, *, scope: str, scope_id: str, category: str, allowed: bool = True
    ) -> PublishGrant:
        existing = (
            await self._session.execute(
                select(PublishGrant).where(
                    PublishGrant.scope == scope,
                    PublishGrant.scope_id == scope_id,
                    PublishGrant.category == category,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.allowed = allowed
            row = existing
        else:
            row = PublishGrant(scope=scope, scope_id=scope_id, category=category, allowed=allowed)
            self._session.add(row)
        await self._session.flush()
        await self._session.commit()
        return row
