from __future__ import annotations

import pytest

from app.core.errors import ComponentNotFoundError
from app.domain.auth import Principal
from app.extensions.loader import Registry
from app.services.entitlement_service import EntitlementService
from tests.unit.factories import make_component, make_template

ADMIN = Principal(tenant_id="tenant-a", user_id="user-a", role="admin")
TENANT_A = Principal(tenant_id="tenant-a", user_id="user-a", role="service")
TENANT_B = Principal(tenant_id="tenant-b", user_id="user-b", role="service")


def _registry_with(public: dict, tenant_a_private: dict | None = None) -> Registry:
    components = dict(public)
    for name, component in (tenant_a_private or {}).items():
        components[f"tenant/tenant-a/{name}"] = component
    return Registry(components=components, templates={})


async def test_admin_sees_everything_unfiltered(db_session) -> None:
    registry = _registry_with(
        public={"python@3.12.4": make_component("python", "3.12.4")},
        tenant_a_private={"mytool@1.0": make_component("mytool", "1.0")},
    )
    service = EntitlementService(db_session)

    visible = await service.filter_components(registry.components, ADMIN)

    assert set(visible) == set(registry.components)


async def test_public_component_hidden_without_entitlement(db_session) -> None:
    registry = _registry_with(public={"python@3.12.4": make_component("python", "3.12.4")})
    service = EntitlementService(db_session)

    visible = await service.filter_components(registry.components, TENANT_A)

    assert visible == {}


async def test_public_component_visible_with_matching_entitlement(db_session) -> None:
    registry = _registry_with(public={"python@3.12.4": make_component("python", "3.12.4")})
    service = EntitlementService(db_session)
    await service.upsert_entitlement(
        scope="tenant", scope_id="tenant-a", component_name="python", version_range="*"
    )

    visible = await service.filter_components(registry.components, TENANT_A)

    assert set(visible) == {"python@3.12.4"}


async def test_entitlement_version_range_exact_match_only(db_session) -> None:
    registry = _registry_with(
        public={
            "python@3.12.4": make_component("python", "3.12.4"),
            "python@3.11.0": make_component("python", "3.11.0"),
        }
    )
    service = EntitlementService(db_session)
    await service.upsert_entitlement(
        scope="tenant", scope_id="tenant-a", component_name="python", version_range="3.12.4"
    )

    visible = await service.filter_components(registry.components, TENANT_A)

    assert set(visible) == {"python@3.12.4"}


async def test_private_component_visible_only_to_owning_tenant(db_session) -> None:
    registry = _registry_with(
        public={},
        tenant_a_private={"mytool@1.0": make_component("mytool", "1.0")},
    )
    service = EntitlementService(db_session)

    visible_to_owner = await service.filter_components(registry.components, TENANT_A)
    visible_to_other = await service.filter_components(registry.components, TENANT_B)

    assert set(visible_to_owner) == {"tenant/tenant-a/mytool@1.0"}
    assert visible_to_other == {}


async def test_bare_lookup_never_matches_private_component() -> None:
    """Regression guard: latest_component("mytool") must never resolve to another
    tenant's private "mytool" component (doc §3.6 — never leak existence cross-tenant)."""
    registry = _registry_with(
        public={}, tenant_a_private={"mytool@1.0": make_component("mytool", "1.0")}
    )

    with pytest.raises(ComponentNotFoundError):
        registry.latest_component("mytool")


async def test_filter_templates_requires_all_refs_visible(db_session) -> None:
    python = make_component("python", "3.12.4")
    git = make_component("git", "1.0", category="tool")
    registry = Registry(
        components={"python@3.12.4": python, "git@1.0": git},
        templates={
            "lab@1.0": make_template(
                "lab", "1.0", base_ref="python@3.12.4", component_refs=["git@1.0"]
            )
        },
    )
    service = EntitlementService(db_session)

    # Neither component entitled yet -> template invisible.
    visible = await service.filter_templates(registry.templates, TENANT_A, registry)
    assert visible == {}

    # Entitle only "python" -> still invisible (git ref unresolved for this caller).
    await service.upsert_entitlement(scope="tenant", scope_id="tenant-a", component_name="python")
    visible = await service.filter_templates(registry.templates, TENANT_A, registry)
    assert visible == {}

    # Entitle "git" too -> now every ref resolves -> template visible.
    await service.upsert_entitlement(scope="tenant", scope_id="tenant-a", component_name="git")
    visible = await service.filter_templates(registry.templates, TENANT_A, registry)
    assert set(visible) == {"lab@1.0"}


async def test_private_template_visible_only_to_owning_tenant(db_session) -> None:
    registry = Registry(
        components={},
        templates={
            "tenant/tenant-a/lab@1.0": make_template(
                "lab", "1.0", base_ref="x@1.0", component_refs=[]
            )
        },
    )
    service = EntitlementService(db_session)

    assert set(await service.filter_templates(registry.templates, TENANT_A, registry)) == {
        "tenant/tenant-a/lab@1.0"
    }
    assert await service.filter_templates(registry.templates, TENANT_B, registry) == {}


async def test_can_publish_requires_grant_unless_admin(db_session) -> None:
    service = EntitlementService(db_session)

    assert await service.can_publish(ADMIN, "language") is True
    assert await service.can_publish(TENANT_A, "language") is False

    await service.upsert_publish_grant(scope="tenant", scope_id="tenant-a", category="language")

    assert await service.can_publish(TENANT_A, "language") is True
    assert await service.can_publish(TENANT_B, "language") is False


async def test_upsert_entitlement_is_idempotent_on_same_key(db_session) -> None:
    service = EntitlementService(db_session)

    first = await service.upsert_entitlement(
        scope="tenant", scope_id="tenant-a", component_name="python", visible=True
    )
    second = await service.upsert_entitlement(
        scope="tenant", scope_id="tenant-a", component_name="python", visible=False
    )

    rows = await service.list_entitlements(scope="tenant", scope_id="tenant-a")
    assert len(rows) == 1
    assert first.id == second.id
    assert rows[0].visible is False
