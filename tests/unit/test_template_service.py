from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import EntitlementError, ManifestValidationError
from app.domain.auth import Principal
from app.extensions.loader import Registry
from app.persistence.models import TemplateRecord
from app.services.entitlement_service import EntitlementService
from app.services.registry_service import RegistryService
from app.services.template_service import TemplateService
from tests.unit.factories import make_component

ADMIN = Principal(tenant_id="tenant-a", user_id="user-a", role="admin")
TENANT_A = Principal(tenant_id="tenant-a", user_id="user-a", role="service")

_RAW_TEMPLATE_MANIFEST = {
    "apiVersion": "kubesandbox.io/v1",
    "kind": "SandboxTemplate",
    "metadata": {"name": "lab", "version": "1.0"},
    "spec": {
        "base": {"ref": "base@1.0"},
        "components": [{"ref": "base@1.0"}],
        "resources": {"cpu": "500m", "memory": "256Mi"},
        "ttl": {"idle": "15m", "max": "2h"},
    },
}


def _services(tmp_path, db_session) -> TemplateService:
    registry = Registry(components={"base@1.0": make_component("base", "1.0")}, templates={})
    entitlements = EntitlementService(db_session)
    return TemplateService(registry, db_session, entitlements, templates_dir=tmp_path)


async def test_admin_creates_template_publicly(tmp_path, db_session) -> None:
    service = _services(tmp_path, db_session)

    key, template = await service.create_template(dict(_RAW_TEMPLATE_MANIFEST), ADMIN)

    assert key == "lab@1.0"
    assert template.metadata.name == "lab"
    assert (tmp_path / "lab" / "1.0.yaml").exists()

    rows = (await db_session.execute(select(TemplateRecord))).scalars().all()
    assert len(rows) == 1
    assert rows[0].name == "lab"


async def test_non_admin_without_grant_is_rejected(tmp_path, db_session) -> None:
    service = _services(tmp_path, db_session)

    with pytest.raises(EntitlementError):
        await service.create_template(dict(_RAW_TEMPLATE_MANIFEST), TENANT_A)


async def test_non_admin_with_grant_creates_privately_namespaced(tmp_path, db_session) -> None:
    service = _services(tmp_path, db_session)
    await service._entitlements.upsert_publish_grant(
        scope="tenant", scope_id="tenant-a", category="template"
    )
    # The template references the public "base" component — publishing a template
    # that depends on it requires being entitled to see it too (not just a "template"
    # publish grant), same as RegistryService's `requires` visibility gate.
    await service._entitlements.upsert_entitlement(
        scope="tenant", scope_id="tenant-a", component_name="base"
    )

    key, template = await service.create_template(dict(_RAW_TEMPLATE_MANIFEST), TENANT_A)

    assert key == "tenant/tenant-a/lab@1.0"
    assert (tmp_path / "tenant" / "tenant-a" / "lab" / "1.0.yaml").exists()


async def test_template_referencing_invisible_component_is_rejected(tmp_path, db_session) -> None:
    service = _services(tmp_path, db_session)
    await service._entitlements.upsert_publish_grant(
        scope="tenant", scope_id="tenant-a", category="template"
    )
    # No component_entitlement for "base" granted -> referencing it must be rejected,
    # even though the publisher may create *some* template.

    with pytest.raises(EntitlementError):
        await service.create_template(dict(_RAW_TEMPLATE_MANIFEST), TENANT_A)


async def test_unresolvable_base_ref_rejected(tmp_path, db_session) -> None:
    service = _services(tmp_path, db_session)
    bad = dict(_RAW_TEMPLATE_MANIFEST)
    bad["spec"] = {**bad["spec"], "base": {"ref": "missing@9.9"}}

    with pytest.raises(ManifestValidationError, match="unresolved component ref"):
        await service.create_template(bad, ADMIN)


async def test_duplicate_template_rejected(tmp_path, db_session) -> None:
    service = _services(tmp_path, db_session)
    await service.create_template(dict(_RAW_TEMPLATE_MANIFEST), ADMIN)

    with pytest.raises(ManifestValidationError, match="already registered"):
        await service.create_template(dict(_RAW_TEMPLATE_MANIFEST), ADMIN)


async def test_list_templates_entitlement_filtered(tmp_path, db_session) -> None:
    service = _services(tmp_path, db_session)
    await service.create_template(dict(_RAW_TEMPLATE_MANIFEST), ADMIN)

    admin_view = await service.list_templates(ADMIN)
    tenant_view = await service.list_templates(TENANT_A)

    assert [key for key, _ in admin_view] == ["lab@1.0"]
    assert tenant_view == []

    await service._entitlements.upsert_entitlement(
        scope="tenant", scope_id="tenant-a", component_name="base"
    )
    tenant_view_after_grant = await service.list_templates(TENANT_A)
    assert [key for key, _ in tenant_view_after_grant] == ["lab@1.0"]
