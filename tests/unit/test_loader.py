from __future__ import annotations

import pytest

from app.core.errors import ComponentNotFoundError, ManifestValidationError
from app.extensions.loader import COMPONENTS_DIR, load_components, load_registry
from tests.conftest import FIXTURES_DIR


def test_real_registry_loads_and_validates() -> None:
    """The actual shipped components/ directory must always load cleanly — this is the
    regression test that catches a broken manifest before it ships."""
    registry = load_registry()
    assert "python@3.12.4" in registry.components
    python = registry.get_component("python", "3.12.4")
    assert python.metadata.category == "language"
    assert python.spec.provides.batchRunner.supportsVariableDump is True


def test_latest_and_ref_resolution_across_versions() -> None:
    registry = load_registry(components_dir=FIXTURES_DIR / "registry_good" / "components")
    latest = registry.latest_component("widget")
    assert latest.metadata.version == "2.0"
    assert registry.resolve_component_ref("widget@latest").metadata.version == "2.0"
    assert registry.resolve_component_ref("widget@1.0").metadata.version == "1.0"


def test_unknown_component_raises() -> None:
    registry = load_registry(components_dir=FIXTURES_DIR / "registry_good" / "components")
    with pytest.raises(ComponentNotFoundError):
        registry.get_component("widget", "9.9")


def test_schema_violation_raises_manifest_validation_error() -> None:
    with pytest.raises(ManifestValidationError):
        load_components(FIXTURES_DIR / "registry_bad_schema" / "components")


def test_unresolved_requires_raises_manifest_validation_error() -> None:
    with pytest.raises(ManifestValidationError):
        load_registry(components_dir=FIXTURES_DIR / "registry_bad_ref" / "components")


def test_default_components_dir_points_at_real_registry() -> None:
    assert COMPONENTS_DIR.name == "components"
    assert (COMPONENTS_DIR / "languages" / "python" / "component.yaml").exists()
