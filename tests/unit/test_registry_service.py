from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import EntitlementError, ManifestValidationError
from app.domain.auth import Principal
from app.extensions.loader import Registry
from app.persistence.models import ComponentRecord
from app.services.entitlement_service import EntitlementService
from app.services.registry_service import RegistryService

ADMIN = Principal(tenant_id="tenant-a", user_id="user-a", role="admin")
TENANT_A = Principal(tenant_id="tenant-a", user_id="user-a", role="service")

_RAW_TOOL_MANIFEST = {
    "apiVersion": "kubesandbox.io/v1",
    "kind": "Component",
    "metadata": {"name": "jq", "version": "1.0", "category": "tool"},
    "spec": {
        "source": {"type": "image", "image": {"repository": "kubesandbox/jq", "tag": "1.0"}},
        "runtime": {
            "kind": "mainTool",
            "resources": {
                "requests": {"cpu": "50m", "memory": "64Mi"},
                "limits": {"cpu": "200m", "memory": "128Mi"},
            },
        },
        "access": {
            "filesystem": {"workdir": "/workspace", "writablePaths": ["/workspace"]},
            "limits": {"processes": 16, "outputBytes": 100000, "wallClockSeconds": 10},
        },
    },
}


def _service(tmp_path, db_session) -> RegistryService:
    registry = Registry(components={}, templates={})
    entitlements = EntitlementService(db_session)
    return RegistryService(registry, db_session, entitlements, components_dir=tmp_path)


async def test_admin_registers_component_publicly(tmp_path, db_session) -> None:
    service = _service(tmp_path, db_session)

    key, component = await service.register_component(dict(_RAW_TOOL_MANIFEST), ADMIN)

    assert key == "jq@1.0"
    assert component.metadata.name == "jq"
    assert (tmp_path / "tools" / "jq" / "1.0" / "component.yaml").exists()
    assert "jq@1.0" in service._registry.components

    rows = (await db_session.execute(select(ComponentRecord))).scalars().all()
    assert len(rows) == 1
    assert rows[0].name == "jq"
    assert rows[0].version == "1.0"


async def test_non_admin_without_grant_is_rejected(tmp_path, db_session) -> None:
    service = _service(tmp_path, db_session)

    with pytest.raises(EntitlementError):
        await service.register_component(dict(_RAW_TOOL_MANIFEST), TENANT_A)

    assert not (tmp_path / "tenant").exists()


async def test_non_admin_with_grant_publishes_privately_namespaced(tmp_path, db_session) -> None:
    service = _service(tmp_path, db_session)
    await service._entitlements.upsert_publish_grant(
        scope="tenant", scope_id="tenant-a", category="tool"
    )

    key, component = await service.register_component(dict(_RAW_TOOL_MANIFEST), TENANT_A)

    assert key == "tenant/tenant-a/jq@1.0"
    assert component.metadata.name == "jq"  # manifest name stays bare/schema-valid
    assert (tmp_path / "tenant" / "tenant-a" / "jq" / "1.0" / "component.yaml").exists()
    assert key in service._registry.components


async def test_duplicate_registration_rejected(tmp_path, db_session) -> None:
    service = _service(tmp_path, db_session)
    await service.register_component(dict(_RAW_TOOL_MANIFEST), ADMIN)

    with pytest.raises(ManifestValidationError, match="already registered"):
        await service.register_component(dict(_RAW_TOOL_MANIFEST), ADMIN)


async def test_invalid_manifest_rejected_before_touching_disk(tmp_path, db_session) -> None:
    service = _service(tmp_path, db_session)
    broken = dict(_RAW_TOOL_MANIFEST)
    broken["metadata"] = {**broken["metadata"], "category": "not-a-real-category"}

    with pytest.raises(ManifestValidationError):
        await service.register_component(broken, ADMIN)

    assert list(tmp_path.iterdir()) == []


async def test_unresolvable_requires_rejected(tmp_path, db_session) -> None:
    service = _service(tmp_path, db_session)
    with_requires = dict(_RAW_TOOL_MANIFEST)
    with_requires["spec"] = {**with_requires["spec"], "requires": ["ghost@9.9"]}

    with pytest.raises(ManifestValidationError, match="unresolved dependency"):
        await service.register_component(with_requires, ADMIN)


async def test_list_and_get_component_versions_are_entitlement_filtered(tmp_path, db_session) -> None:
    service = _service(tmp_path, db_session)
    await service.register_component(dict(_RAW_TOOL_MANIFEST), ADMIN)

    admin_view = await service.list_components(ADMIN)
    tenant_view = await service.list_components(TENANT_A)

    assert [key for key, _ in admin_view] == ["jq@1.0"]
    assert tenant_view == []

    await service._entitlements.upsert_entitlement(
        scope="tenant", scope_id="tenant-a", component_name="jq"
    )
    tenant_view_after_grant = await service.get_component_versions("jq", TENANT_A)
    assert [key for key, _ in tenant_view_after_grant] == ["jq@1.0"]


async def test_registering_a_second_version_does_not_clobber_the_first(tmp_path, db_session) -> None:
    service = _service(tmp_path, db_session)
    await service.register_component(dict(_RAW_TOOL_MANIFEST), ADMIN)

    v2 = dict(_RAW_TOOL_MANIFEST)
    v2["metadata"] = {**v2["metadata"], "version": "2.0"}
    await service.register_component(v2, ADMIN)

    assert (tmp_path / "tools" / "jq" / "1.0" / "component.yaml").exists()
    assert (tmp_path / "tools" / "jq" / "2.0" / "component.yaml").exists()
    assert {key for key, _ in await service.list_components(ADMIN)} == {"jq@1.0", "jq@2.0"}


async def test_get_component_versions_sorts_semantically_not_lexically(tmp_path, db_session) -> None:
    service = _service(tmp_path, db_session)
    for version in ("2.0", "10.0", "9.0"):
        manifest = dict(_RAW_TOOL_MANIFEST)
        manifest["metadata"] = {**manifest["metadata"], "version": version}
        await service.register_component(manifest, ADMIN)

    versions = await service.get_component_versions("jq", ADMIN)

    assert [key for key, _ in versions] == ["jq@10.0", "jq@9.0", "jq@2.0"]


async def test_requires_ref_to_invisible_private_component_is_rejected(tmp_path, db_session) -> None:
    """A non-admin publisher can't smuggle in a `requires` dependency on a component
    they aren't entitled to see, even if it resolves (e.g. guessing another tenant's
    private component ref)."""
    service = _service(tmp_path, db_session)
    other_tenant = Principal(tenant_id="tenant-b", user_id="user-b", role="service")
    await service._entitlements.upsert_publish_grant(
        scope="tenant", scope_id="tenant-b", category="tool"
    )
    await service.register_component(dict(_RAW_TOOL_MANIFEST), other_tenant)  # -> tenant/tenant-b/jq@1.0

    await service._entitlements.upsert_publish_grant(
        scope="tenant", scope_id="tenant-a", category="tool"
    )
    dependent = dict(_RAW_TOOL_MANIFEST)
    dependent["metadata"] = {**dependent["metadata"], "name": "jq2"}
    dependent["spec"] = {**dependent["spec"], "requires": ["tenant/tenant-b/jq@1.0"]}

    with pytest.raises(EntitlementError):
        await service.register_component(dependent, TENANT_A)
