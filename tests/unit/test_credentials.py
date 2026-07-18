from __future__ import annotations

from app.domain.manifests import DatabaseAccess
from app.services.credentials import generate_db_credentials
from tests.unit.factories import make_component


def test_generate_db_credentials_uses_declared_role():
    component = make_component(
        "postgresql", "16", kind="sidecar", category="database", uid=999,
        database=DatabaseAccess(role="app_role"),
    )
    credentials = generate_db_credentials(component)
    assert credentials.role == "app_role"


def test_generate_db_credentials_defaults_role_when_undeclared():
    component = make_component("postgresql", "16", kind="sidecar", category="database", uid=999)
    credentials = generate_db_credentials(component)
    assert credentials.role == "sandbox_user"
    assert credentials.database == "sandbox"
    assert credentials.password
    assert credentials.admin_password
    assert credentials.password != credentials.admin_password


def test_generate_db_credentials_two_calls_never_collide():
    component = make_component("postgresql", "16", kind="sidecar", category="database", uid=999)
    a = generate_db_credentials(component)
    b = generate_db_credentials(component)
    assert a.password != b.password
    assert a.admin_password != b.admin_password
