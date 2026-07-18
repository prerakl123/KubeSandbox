"""Loads Component/SandboxTemplate manifests from the git-backed registry directories,
validates each against its JSON Schema, then against cross-manifest semantics (do
`requires`/`ref` pointers actually resolve), and hands back an in-memory Registry.

The registry directory tree (git) is the source of truth (doc §3.5); Postgres later
holds an indexed projection for querying, refreshed from this loader.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import yaml

from app.core.errors import (
    ComponentNotFoundError,
    ManifestValidationError,
    TemplateNotFoundError,
)
from app.domain.manifests import Component, SandboxTemplate

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "schemas"
COMPONENTS_DIR = REPO_ROOT / "components"
TEMPLATES_DIR = REPO_ROOT / "templates"


def _load_schema(filename: str) -> dict:
    return json.loads((SCHEMAS_DIR / filename).read_text())


_COMPONENT_SCHEMA = _load_schema("component.schema.json")
_TEMPLATE_SCHEMA = _load_schema("template.schema.json")


def _validate_against_schema(document: dict, schema: dict, *, source: Path) -> None:
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(
            f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors
        )
        raise ManifestValidationError(f"{source}: {details}")


def _version_sort_key(version: str) -> tuple[int, ...]:
    core = version.split("+")[0].split("-")[0]
    return tuple(int(part) for part in core.split(".") if part.isdigit())


@dataclass
class Registry:
    components: dict[str, Component] = field(default_factory=dict)  # key: "name@version"
    templates: dict[str, SandboxTemplate] = field(default_factory=dict)

    def get_component(self, name: str, version: str) -> Component:
        key = f"{name}@{version}"
        try:
            return self.components[key]
        except KeyError:
            raise ComponentNotFoundError(key) from None

    def latest_component(self, name: str) -> Component:
        candidates = [c for c in self.components.values() if c.metadata.name == name]
        if not candidates:
            raise ComponentNotFoundError(name)
        return max(candidates, key=lambda c: _version_sort_key(c.metadata.version))

    def resolve_component_ref(self, ref: str) -> Component:
        """ref is 'name@version' or 'name@latest'."""
        name, sep, version = ref.partition("@")
        if not sep or not version:
            raise ComponentNotFoundError(ref)
        if version == "latest":
            return self.latest_component(name)
        return self.get_component(name, version)

    def get_template(self, name: str, version: str) -> SandboxTemplate:
        key = f"{name}@{version}"
        try:
            return self.templates[key]
        except KeyError:
            raise TemplateNotFoundError(key) from None

    def list_components(self) -> list[Component]:
        return list(self.components.values())

    def list_templates(self) -> list[SandboxTemplate]:
        return list(self.templates.values())


def load_components(directory: Path = COMPONENTS_DIR) -> dict[str, Component]:
    components: dict[str, Component] = {}
    if not directory.exists():
        return components
    for path in sorted(directory.rglob("component.yaml")):
        raw = yaml.safe_load(path.read_text())
        _validate_against_schema(raw, _COMPONENT_SCHEMA, source=path)
        component = Component.model_validate(raw)
        if component.key in components:
            raise ManifestValidationError(f"duplicate component {component.key!r} at {path}")
        components[component.key] = component
    return components


def load_templates(directory: Path = TEMPLATES_DIR) -> dict[str, SandboxTemplate]:
    templates: dict[str, SandboxTemplate] = {}
    if not directory.exists():
        return templates
    for path in sorted(directory.rglob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        _validate_against_schema(raw, _TEMPLATE_SCHEMA, source=path)
        template = SandboxTemplate.model_validate(raw)
        if template.key in templates:
            raise ManifestValidationError(f"duplicate template {template.key!r} at {path}")
        templates[template.key] = template
    return templates


def validate_registry_semantics(registry: Registry) -> None:
    """Cross-manifest checks JSON Schema can't express: do refs actually resolve?"""
    for component in registry.components.values():
        for req in component.spec.requires:
            try:
                registry.resolve_component_ref(req)
            except ComponentNotFoundError as exc:
                raise ManifestValidationError(
                    f"component {component.key} requires unresolved dependency {req!r}"
                ) from exc

    for template in registry.templates.values():
        try:
            registry.resolve_component_ref(template.spec.base.ref)
        except ComponentNotFoundError as exc:
            raise ManifestValidationError(
                f"template {template.key} has unresolved base ref {template.spec.base.ref!r}"
            ) from exc
        for component_ref in template.spec.components:
            try:
                registry.resolve_component_ref(component_ref.ref)
            except ComponentNotFoundError as exc:
                raise ManifestValidationError(
                    f"template {template.key} has unresolved component ref {component_ref.ref!r}"
                ) from exc


def load_registry(
    components_dir: Path = COMPONENTS_DIR, templates_dir: Path = TEMPLATES_DIR
) -> Registry:
    registry = Registry(
        components=load_components(components_dir),
        templates=load_templates(templates_dir),
    )
    validate_registry_semantics(registry)
    return registry
