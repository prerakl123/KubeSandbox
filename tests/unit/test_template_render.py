from __future__ import annotations

import pytest

from app.core.errors import ComponentNotFoundError, KubeSandboxError
from app.domain.execution import WeightClass
from app.domain.manifests import EnvVar
from app.extensions.loader import Registry, load_registry
from app.services.template_render import render_template
from tests.unit.factories import make_component, make_template


def test_real_base_dev_lab_template_renders_with_shared_image() -> None:
    """The actual shipped templates/base-dev-lab.yaml must render end-to-end: base +
    bash + git all resolve to the one shared golden image (kubesandbox/base:1.0)."""
    registry = load_registry()
    template = registry.resolve_template_ref("base-dev-lab@1.0")

    rendered = render_template(registry, template)

    assert rendered.sandbox_spec.image == "kubesandbox/base:1.0"
    assert {c.key for c in rendered.main_components} == {"base@1.0", "bash@1.0", "git@1.0"}
    assert rendered.sidecar_components == []


def test_render_merges_env_and_writable_paths_across_components() -> None:
    base = make_component(
        "base", "1.0", env=[EnvVar(name="A", value="1")], writable_paths=["/workspace"]
    )
    extra = make_component(
        "extra", "1.0", env=[EnvVar(name="B", value="2")], writable_paths=["/tmp"]
    )
    registry = Registry(components={"base@1.0": base, "extra@1.0": extra}, templates={})
    template = make_template("t", "1.0", base_ref="base@1.0", component_refs=["extra@1.0"])

    rendered = render_template(registry, template)

    assert rendered.sandbox_spec.env == {"A": "1", "B": "2"}
    assert rendered.sandbox_spec.writable_paths == ["/workspace", "/tmp"]
    assert rendered.sandbox_spec.image == "kubesandbox/x:1"


def test_render_rejects_mismatched_images() -> None:
    base = make_component("base", "1.0", image_repo="kubesandbox/a", image_tag="1")
    other = make_component("other", "1.0", image_repo="kubesandbox/b", image_tag="1")
    registry = Registry(components={"base@1.0": base, "other@1.0": other}, templates={})
    template = make_template("t", "1.0", base_ref="base@1.0", component_refs=["other@1.0"])

    with pytest.raises(KubeSandboxError, match="distinct images"):
        render_template(registry, template)


def test_render_weight_class_defaults_to_max_across_components() -> None:
    base = make_component("base", "1.0", weight="light")
    heavy = make_component("heavy", "1.0", weight="heavy")
    registry = Registry(components={"base@1.0": base, "heavy@1.0": heavy}, templates={})
    template = make_template("t", "1.0", base_ref="base@1.0", component_refs=["heavy@1.0"])

    rendered = render_template(registry, template)

    assert rendered.sandbox_spec.weight_class == WeightClass.HEAVY


def test_render_separates_sidecar_components() -> None:
    base = make_component("base", "1.0")
    sidecar = make_component("db", "1.0", kind="sidecar", category="database")
    registry = Registry(components={"base@1.0": base, "db@1.0": sidecar}, templates={})
    template = make_template("t", "1.0", base_ref="base@1.0", component_refs=["db@1.0"])

    rendered = render_template(registry, template)

    assert [c.key for c in rendered.main_components] == ["base@1.0"]
    assert [c.key for c in rendered.sidecar_components] == ["db@1.0"]


def test_render_unresolvable_base_ref_raises() -> None:
    registry = Registry(components={}, templates={})
    template = make_template("t", "1.0", base_ref="missing@1.0", component_refs=[])

    with pytest.raises(ComponentNotFoundError):
        render_template(registry, template)
