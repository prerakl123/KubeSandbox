"""Unit-testable slices of the four BuildStrategy implementations (doc §8, Phase 6) —
the parts that don't need a real Docker daemon/subprocess/helm binary: compose-file
parsing, pipeline step sequencing + fail-fast + cache-hit-skip, and helm's
missing-binary error path. The actual `docker build`/`helm template` calls are only
exercised by the self-skipping integration tests (tests/integration/), the same split
Phase 1's `_tar_context`/DockerProvisioner already established.
"""

from __future__ import annotations

import pytest
import yaml

from app.build.strategies.compose import ComposeBuildStrategy, parse_compose_services, select_primary_service
from app.build.strategies.helm import HelmChartStrategy
from app.build.strategies.pipeline import PipelineBuildStrategy
from app.core.errors import BuildError
from app.domain.build import BuildContext
from app.domain.manifests import (
    ComponentSource,
    ComposeSource,
    HelmSource,
    PipelineCache,
    PipelineSource,
    PipelineStep,
)
from tests.unit.factories import make_build_component
from tests.unit.fakes import FakeObjectStorageProvider

# --- compose parsing (pure) ---------------------------------------------------------


def test_parse_compose_services_returns_services_dict() -> None:
    raw = yaml.safe_load(
        """
        services:
          ripgrep:
            build: { context: ., dockerfile: Dockerfile }
        """
    )
    services = parse_compose_services(raw)
    assert set(services) == {"ripgrep"}


def test_parse_compose_services_rejects_empty() -> None:
    with pytest.raises(BuildError, match="no services"):
        parse_compose_services({"services": {}})
    with pytest.raises(BuildError, match="no services"):
        parse_compose_services({})


def test_select_primary_service_prefers_matching_component_name() -> None:
    services = {"other": {}, "ripgrep": {}}
    assert select_primary_service(services, "ripgrep") == "ripgrep"


def test_select_primary_service_falls_back_to_first() -> None:
    services = {"only-one": {}}
    assert select_primary_service(services, "does-not-match") == "only-one"


async def test_compose_strategy_records_non_primary_services(tmp_path, monkeypatch) -> None:
    (tmp_path / "docker-compose.yaml").write_text(
        yaml.safe_dump(
            {
                "services": {
                    "ripgrep": {"build": {"context": ".", "dockerfile": "Dockerfile"}},
                    "sidecar-tool": {"image": "prebuilt/sidecar:1.0"},
                }
            }
        )
    )

    built_calls = []

    async def fake_build_image_from_dockerfile(component_dir, *, context, dockerfile_path, local_tag, log):
        built_calls.append(local_tag)
        return local_tag

    import app.build.strategies.compose as compose_mod

    monkeypatch.setattr(compose_mod, "build_image_from_dockerfile", fake_build_image_from_dockerfile)

    component = make_build_component(
        "ripgrep", "1.0", source=ComponentSource(type="compose", compose=ComposeSource())
    )
    ctx = BuildContext(component_dir=tmp_path, build_id="b1", image_repo="kubesandbox/ripgrep", image_tag="1.0")

    artifact = await ComposeBuildStrategy().build(component, ctx)

    assert artifact.kind == "image"
    assert artifact.ref == "kubesandbox/ripgrep:1.0"
    assert artifact.metadata["services"] == {"sidecar-tool": "prebuilt/sidecar:1.0"}
    assert built_calls == ["kubesandbox/ripgrep:1.0"]


# --- pipeline step sequencing / fail-fast / caching ---------------------------------


def _pipeline_component(steps: list[PipelineStep], cache: PipelineCache | None = None):
    return make_build_component(
        "httpie", "1.0",
        source=ComponentSource(type="pipeline", pipeline=PipelineSource(steps=steps, cache=cache)),
    )


async def test_pipeline_runs_steps_in_order_then_packages(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_runner(command, cwd, env, log):
        calls.append(command)
        log.append(f"ran {command}")

    async def fake_build_image(component_dir, *, context, dockerfile_path, local_tag, log):
        calls.append(f"package:{local_tag}")
        return local_tag

    import app.build.strategies.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "build_image_from_dockerfile", fake_build_image)

    component = _pipeline_component(
        [PipelineStep(name="prepare", run="step-one"), PipelineStep(name="build", run="step-two")]
    )
    ctx = BuildContext(component_dir=tmp_path, build_id="b1", image_repo="kubesandbox/httpie", image_tag="1.0")

    strategy = PipelineBuildStrategy(step_runner=fake_runner)
    artifact = await strategy.build(component, ctx)

    assert calls == ["step-one", "step-two", "package:kubesandbox/httpie:1.0"]
    assert artifact.ref == "kubesandbox/httpie:1.0"


async def test_pipeline_fails_fast_on_first_bad_step(tmp_path) -> None:
    calls: list[str] = []

    async def failing_runner(command, cwd, env, log):
        calls.append(command)
        raise BuildError(f"step failed: {command}")

    component = _pipeline_component(
        [PipelineStep(name="prepare", run="bad-step"), PipelineStep(name="build", run="never-runs")]
    )
    ctx = BuildContext(component_dir=tmp_path, build_id="b1", image_repo="kubesandbox/httpie", image_tag="1.0")

    strategy = PipelineBuildStrategy(step_runner=failing_runner)
    with pytest.raises(BuildError, match="bad-step"):
        await strategy.build(component, ctx)

    assert calls == ["bad-step"]  # never reached the second step


async def test_pipeline_cache_hit_skips_steps(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_runner(command, cwd, env, log):
        calls.append(command)

    async def fake_build_image(component_dir, *, context, dockerfile_path, local_tag, log):
        calls.append(f"package:{local_tag}")
        return local_tag

    import app.build.strategies.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "build_image_from_dockerfile", fake_build_image)

    component = _pipeline_component(
        [PipelineStep(name="prepare", run="step-one")],
        cache=PipelineCache(key="{name}-{version}", store="object"),
    )
    object_storage = FakeObjectStorageProvider()
    ctx = BuildContext(
        component_dir=tmp_path, build_id="b1", image_repo="kubesandbox/httpie", image_tag="1.0",
        object_storage=object_storage,
    )
    strategy = PipelineBuildStrategy(step_runner=fake_runner)

    first = await strategy.build(component, ctx)
    assert len(calls) == 2  # one step + package
    assert first.metadata == {}

    second = await strategy.build(component, ctx)
    assert len(calls) == 2  # unchanged — steps were skipped on cache hit
    assert second.metadata == {"cache_hit": True}
    assert second.ref == first.ref


async def test_pipeline_cache_declared_without_object_storage_raises(tmp_path) -> None:
    component = _pipeline_component(
        [PipelineStep(name="prepare", run="step-one")],
        cache=PipelineCache(key="{name}-{version}", store="object"),
    )
    ctx = BuildContext(
        component_dir=tmp_path, build_id="b1", image_repo="kubesandbox/httpie", image_tag="1.0",
        object_storage=None,
    )

    async def unused_runner(command, cwd, env, log):
        raise AssertionError("should never run — object storage check happens first")

    strategy = PipelineBuildStrategy(step_runner=unused_runner)
    with pytest.raises(BuildError, match="ObjectStorageProvider"):
        await strategy.build(component, ctx)


# --- helm missing-binary path --------------------------------------------------------


async def test_helm_strategy_raises_clear_error_when_helm_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    component = make_build_component(
        "demo-echo", "1.0", category="service",
        source=ComponentSource(type="helm", helm=HelmSource(chart="chart")),
    )
    ctx = BuildContext(
        component_dir=tmp_path, build_id="b1", image_repo="kubesandbox/demo-echo", image_tag="1.0",
        object_storage=FakeObjectStorageProvider(),
    )

    with pytest.raises(BuildError, match="helm.*not found"):
        await HelmChartStrategy().build(component, ctx)


async def test_helm_strategy_requires_object_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/helm")
    component = make_build_component(
        "demo-echo", "1.0", category="service",
        source=ComponentSource(type="helm", helm=HelmSource(chart="chart")),
    )
    ctx = BuildContext(
        component_dir=tmp_path, build_id="b1", image_repo="kubesandbox/demo-echo", image_tag="1.0",
        object_storage=None,
    )

    with pytest.raises(BuildError, match="ObjectStorageProvider"):
        await HelmChartStrategy().build(component, ctx)
