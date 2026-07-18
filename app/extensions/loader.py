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

# Public aliases: the API layer serves these back verbatim (GET /v1/components/{name})
# so manifest authors can validate client-side against the same schema the loader uses.
COMPONENT_SCHEMA = _COMPONENT_SCHEMA
TEMPLATE_SCHEMA = _TEMPLATE_SCHEMA


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


def version_sort_key(version: str) -> tuple[int, ...]:
    """Public so callers outside the loader (e.g. RegistryService) can sort a list of
    version strings the same semantically-correct way as latest_component() does —
    plain string sort puts "9.0" ahead of "10.0"."""
    core = version.split("+")[0].split("-")[0]
    return tuple(int(part) for part in core.split(".") if part.isdigit())


@dataclass
class Registry:
    """`components`/`templates` are keyed by a *registry key*, not always the bare
    manifest name: public entries key on `metadata.name@version` as before, but
    tenant-private entries (loaded from a `tenant/<tenant_id>/...` subtree, doc §3.6)
    key on `tenant/<tenant_id>/<name>@version` instead — see `_registry_key_for`. This
    keeps private components/templates from colliding with (or being reachable via a
    bare lookup that could leak their existence across) another tenant's catalog.
    """

    components: dict[str, Component] = field(default_factory=dict)
    templates: dict[str, SandboxTemplate] = field(default_factory=dict)

    def get_component(self, name: str, version: str) -> Component:
        key = f"{name}@{version}"
        try:
            return self.components[key]
        except KeyError:
            raise ComponentNotFoundError(key) from None

    def latest_component(self, name: str) -> Component:
        # Matched against the registry-key's name portion, not metadata.name: a
        # tenant-private component's metadata.name is an unqualified bare name (e.g.
        # "mytool", schema-valid), so matching on metadata.name here would let a bare
        # lookup for "mytool" resolve to (and thus leak the existence of) another
        # tenant's private component of the same name. Matching on the qualified key
        # means a bare lookup only ever considers public entries.
        candidates = [
            c for key, c in self.components.items() if key.rsplit("@", 1)[0] == name
        ]
        if not candidates:
            raise ComponentNotFoundError(name)
        return max(candidates, key=lambda c: version_sort_key(c.metadata.version))

    def resolve_component_ref(self, ref: str) -> Component:
        """ref is 'name@version' or 'name@latest' — name may be a bare public name or
        a qualified 'tenant/<id>/<name>' private one; both are just registry keys."""
        name, sep, version = ref.rpartition("@")
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

    def latest_template(self, name: str) -> SandboxTemplate:
        candidates = [
            t for key, t in self.templates.items() if key.rsplit("@", 1)[0] == name
        ]
        if not candidates:
            raise TemplateNotFoundError(name)
        return max(candidates, key=lambda t: version_sort_key(t.metadata.version))

    def resolve_template_ref(self, ref: str) -> SandboxTemplate:
        """ref is 'name@version' or 'name@latest', mirroring resolve_component_ref."""
        name, sep, version = ref.rpartition("@")
        if not sep or not version:
            raise TemplateNotFoundError(ref)
        if version == "latest":
            return self.latest_template(name)
        return self.get_template(name, version)

    def list_components(self) -> list[Component]:
        return list(self.components.values())

    def list_templates(self) -> list[SandboxTemplate]:
        return list(self.templates.values())


def registry_key_for(path: Path, bare_key: str, root: Path) -> str:
    """Public components/templates key on their bare 'name@version'. Anything found
    under a '<root>/tenant/<tenant_id>/...' subtree (doc §3.6) is scope-owned and keys
    on 'tenant/<tenant_id>/name@version' instead, so it can never collide with — or be
    reached by a bare lookup for — another tenant's (or the public catalog's) entry."""
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return bare_key
    if len(rel_parts) >= 2 and rel_parts[0] == "tenant":
        return f"tenant/{rel_parts[1]}/{bare_key}"
    return bare_key


def validate_component_manifest(raw: dict, *, source: Path) -> Component:
    """Validate a raw Component manifest dict against the JSON Schema, then parse it
    into the typed model. Public so services (e.g. RegistryService) can validate a
    manifest submitted through the API the same way the disk loader does, before
    persisting it."""
    _validate_against_schema(raw, _COMPONENT_SCHEMA, source=source)
    return Component.model_validate(raw)


def validate_template_manifest(raw: dict, *, source: Path) -> SandboxTemplate:
    """Template-manifest counterpart to validate_component_manifest."""
    _validate_against_schema(raw, _TEMPLATE_SCHEMA, source=source)
    return SandboxTemplate.model_validate(raw)


def load_components(directory: Path = COMPONENTS_DIR) -> dict[str, Component]:
    components: dict[str, Component] = {}
    if not directory.exists():
        return components
    for path in sorted(directory.rglob("component.yaml")):
        raw = yaml.safe_load(path.read_text())
        component = validate_component_manifest(raw, source=path)
        key = registry_key_for(path, component.key, directory)
        if key in components:
            raise ManifestValidationError(f"duplicate component {key!r} at {path}")
        components[key] = component
    return components


def load_templates(directory: Path = TEMPLATES_DIR) -> dict[str, SandboxTemplate]:
    templates: dict[str, SandboxTemplate] = {}
    if not directory.exists():
        return templates
    for path in sorted(directory.rglob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        template = validate_template_manifest(raw, source=path)
        key = registry_key_for(path, template.key, directory)
        if key in templates:
            raise ManifestValidationError(f"duplicate template {key!r} at {path}")
        templates[key] = template
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
