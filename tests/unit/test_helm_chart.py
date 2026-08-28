"""Static checks on the control-plane Helm chart (`deploy/helm/kubesandbox`, Phase 9).

`helm lint`/`helm template` is the real validation and is not available in this
environment (no `helm` binary — see the Phase 9 notes in docs/TASK_CHECKLIST.md). These
tests cover the failure mode that `helm lint` *doesn't* catch anyway and that costs the
most to debug: **a `.Values` path that doesn't exist**. Go templates render a missing key
as the empty string rather than failing, so a typo like `.Values.config.corse.enabled`
produces a syntactically valid manifest with a silently-wrong value — a deployment that
comes up with CORS off and no error anywhere.

Also checked: every `if`/`range`/`with` block is closed, and the invariants the chart's
own comments claim (reconciler pinned to one replica, no HPA over it, ClusterIP service)
actually hold in the templates rather than only in prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.conftest import REPO_ROOT

CHART_DIR = REPO_ROOT / "deploy" / "helm" / "kubesandbox"
TEMPLATES_DIR = CHART_DIR / "templates"

# Paths supplied by Helm itself or by the chart's own `define` blocks, not by values.yaml.
_BUILTIN_PREFIXES = ("Values.global",)

_VALUES_REF = re.compile(r"\.Values\.([A-Za-z0-9_.]+)")
_BLOCK_OPEN = re.compile(r"\{\{-?\s*(if|range|with|define)\b")
_BLOCK_CLOSE = re.compile(r"\{\{-?\s*end\s*-?\}\}")


def _template_files() -> list[Path]:
    return sorted(TEMPLATES_DIR.glob("*.yaml")) + sorted(TEMPLATES_DIR.glob("*.tpl"))


def _flatten(node, prefix: str = "") -> set[str]:
    """Every addressable dotted path in values.yaml, including intermediate ones.

    Intermediate paths matter because a template legitimately references a whole subtree
    (`with .Values.nodeSelector`, `toYaml .Values.resources`), not only leaves.
    """
    paths: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.add(path)
            paths |= _flatten(value, path)
    return paths


@pytest.fixture(scope="module")
def values() -> dict:
    return yaml.safe_load((CHART_DIR / "values.yaml").read_text())


@pytest.fixture(scope="module")
def value_paths(values) -> set[str]:
    return _flatten(values)


def test_the_chart_has_the_files_helm_requires() -> None:
    assert (CHART_DIR / "Chart.yaml").exists()
    assert (CHART_DIR / "values.yaml").exists()
    assert (TEMPLATES_DIR / "_helpers.tpl").exists()
    assert (TEMPLATES_DIR / "NOTES.txt").exists()


def test_chart_metadata_is_well_formed() -> None:
    chart = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text())
    assert chart["apiVersion"] == "v2"
    assert chart["name"] == "kubesandbox"
    assert chart["version"] and chart["appVersion"]


def test_every_values_reference_exists_in_values_yaml(value_paths) -> None:
    """The point of this whole module. A missing `.Values` path renders as an empty
    string, so the manifest is valid and the setting is silently wrong."""
    unknown: list[str] = []
    for path in _template_files():
        text = path.read_text()
        for match in _VALUES_REF.finditer(text):
            # Trailing dots come from constructs like `.Values.config.cors.enabled }}`
            # caught mid-expression; normalize before comparing.
            ref = match.group(1).rstrip(".")
            if ref.startswith(_BUILTIN_PREFIXES):
                continue
            if ref not in value_paths:
                unknown.append(f"{path.name}: .Values.{ref}")
    assert not unknown, "values paths referenced by templates but absent from values.yaml:\n" + "\n".join(unknown)


def test_every_values_reference_also_exists_in_the_local_overrides() -> None:
    """values-local.yaml is an override file, not a full replacement, so it only needs to
    be a *subset* — but a path in it that doesn't exist in values.yaml is a typo that
    silently does nothing."""
    base = _flatten(yaml.safe_load((CHART_DIR / "values.yaml").read_text()))
    local = _flatten(yaml.safe_load((CHART_DIR / "values-local.yaml").read_text()))
    assert local <= base, f"values-local.yaml has paths absent from values.yaml: {sorted(local - base)}"


def test_template_control_blocks_are_balanced() -> None:
    """An unclosed `if` fails `helm template` with a message that points at the end of the
    file rather than the mistake — cheap to catch here instead."""
    unbalanced: list[str] = []
    for path in _template_files():
        text = path.read_text()
        opens = len(_BLOCK_OPEN.findall(text))
        closes = len(_BLOCK_CLOSE.findall(text))
        if opens != closes:
            unbalanced.append(f"{path.name}: {opens} open, {closes} end")
    assert not unbalanced, "unbalanced template blocks:\n" + "\n".join(unbalanced)


def test_no_template_uses_a_double_brace_without_closing() -> None:
    for path in _template_files():
        text = path.read_text()
        assert text.count("{{") == text.count("}}"), f"{path.name}: unbalanced braces"


# -- invariants the chart's comments claim -------------------------------------------


def test_the_reconciler_is_pinned_to_one_replica(values) -> None:
    """Its jobs (TTL reaping, orphan GC, pool replenishment) are not distributed-safe —
    two replicas would both reap the same sandbox. Asserted so a well-meaning bump can't
    slip through."""
    assert values["reconciler"]["replicaCount"] == 1


def _strip_comments(text: str) -> str:
    """Drop Go-template comments (`{{/* ... */}}`) and YAML `#` comments.

    Needed because these templates document their own reasoning inline, so a naive
    substring search hits the explanation of why something is absent rather than the
    thing itself — e.g. hpa.yaml's comment says the word "reconciler" precisely to say
    it does not target it.
    """
    text = re.sub(r"\{\{/\*.*?\*/\}\}", "", text, flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_no_hpa_targets_the_reconciler() -> None:
    hpa = _strip_comments((TEMPLATES_DIR / "hpa.yaml").read_text())
    assert "reconciler" not in hpa


def test_the_reconciler_uses_recreate_not_rolling_update() -> None:
    """With one replica, a rolling update briefly runs old and new together — exactly the
    concurrent-tick situation the single replica exists to prevent."""
    text = (TEMPLATES_DIR / "deployment-reconciler.yaml").read_text()
    assert "type: Recreate" in text


def test_the_service_is_clusterip_and_selects_only_the_api(values) -> None:
    """Doc §12 puts all interaction behind the control plane; a LoadBalancer here would
    put the API on the public internet with no TLS. And the selector must exclude the
    reconciler, which shares the name/instance labels but serves no HTTP."""
    assert values["service"]["type"] == "ClusterIP"
    service = (TEMPLATES_DIR / "service.yaml").read_text()
    assert "app.kubernetes.io/component: api" in service


def test_the_api_rollout_never_reduces_capacity() -> None:
    text = (TEMPLATES_DIR / "deployment-api.yaml").read_text()
    assert "maxUnavailable: 0" in text


def test_pod_security_context_matches_doc_6_layer_1(values) -> None:
    """The control plane holds the credentials that can create pods, so §6 Layer 1
    applies to it too, not only to sandboxes."""
    pod = values["podSecurityContext"]
    container = values["securityContext"]
    assert pod["runAsNonRoot"] is True
    assert pod["runAsUser"] == 10001
    assert pod["seccompProfile"]["type"] == "RuntimeDefault"
    assert container["allowPrivilegeEscalation"] is False
    assert container["readOnlyRootFilesystem"] is True
    assert container["capabilities"]["drop"] == ["ALL"]


def test_read_only_root_filesystem_has_matching_scratch_volumes() -> None:
    """`readOnlyRootFilesystem: true` breaks anything that writes — /tmp is used for
    tarball staging on file upload/download, and uv needs its cache dir."""
    helpers = (TEMPLATES_DIR / "_helpers.tpl").read_text()
    assert "mountPath: /tmp" in helpers
    assert "uv-cache" in helpers


def test_ingress_and_cors_are_off_by_default(values) -> None:
    """Neither should be enabled by an install that didn't ask: one publishes the API,
    the other opens it to a browser origin."""
    assert values["ingress"]["enabled"] is False
    assert values["config"]["cors"]["enabled"] is False


def test_ingress_annotations_cover_websocket_and_long_runs(values) -> None:
    """A default 60s proxy read timeout kills an idle terminal every minute and 504s a
    batch run that is still legitimately executing."""
    annotations = values["ingress"]["annotations"]
    assert any("proxy-read-timeout" in key for key in annotations)
    assert int(annotations["nginx.ingress.kubernetes.io/proxy-read-timeout"]) >= 600


def test_image_tag_is_pinned_and_pull_policy_is_not_always(values) -> None:
    """Doc §8's premise is immutable, fully-baked images; a mutable tag means two
    replicas of "the same" release can run different code after a restart."""
    assert values["image"]["tag"] != "latest"
    assert values["image"]["pullPolicy"] != "Always"


def test_migrations_run_as_a_hook_not_an_init_container() -> None:
    """With N replicas an init container runs the migration N times concurrently, and
    Alembic is not safe under that."""
    text = (TEMPLATES_DIR / "job-migrate.yaml").read_text()
    assert "helm.sh/hook: pre-install,pre-upgrade" in text
    assert "initContainers" not in text


def test_the_migration_hook_keeps_failed_pods_for_diagnosis() -> None:
    """When a migration fails, its pod and logs are the only diagnostic."""
    text = _strip_comments((TEMPLATES_DIR / "job-migrate.yaml").read_text())
    assert "hook-failed" not in text


def test_rbac_grants_no_secrets_access_and_no_wildcards() -> None:
    """This identity can create pods cluster-wide; the blast radius of a wildcard here is
    the whole cluster."""
    text = _strip_comments((TEMPLATES_DIR / "rbac.yaml").read_text())
    assert '"*"' not in text
    # No `secrets` resource in any rule. Checked against the comment-stripped body
    # because the template's own header explains this absence in prose.
    assert '"secrets"' not in text
    assert "resources: [\"secrets\"]" not in text


def test_secret_env_is_marked_optional() -> None:
    """The Key Vault CSI driver only syncs its Secret once a pod mounts the volume — a
    required secretKeyRef would deadlock the first pod waiting for a Secret whose
    creation depends on that pod starting."""
    helpers = (TEMPLATES_DIR / "_helpers.tpl").read_text()
    assert helpers.count("optional: true") >= 3


def test_the_api_deployment_omits_replicas_when_autoscaling(values) -> None:
    """Setting `replicas` alongside an HPA makes every `helm upgrade` reset the count and
    undo whatever the HPA had scaled to."""
    text = (TEMPLATES_DIR / "deployment-api.yaml").read_text()
    assert "if not .Values.autoscaling.enabled" in text
    assert values["autoscaling"]["enabled"] is True


def test_config_rolls_pods_on_change() -> None:
    """Without a checksum annotation, a `helm upgrade` that only edits config leaves
    every replica running the old settings."""
    for name in ("deployment-api.yaml", "deployment-reconciler.yaml"):
        assert "checksum/config" in (TEMPLATES_DIR / name).read_text(), name


def test_notes_warn_about_the_two_ui_blockers() -> None:
    """CORS disabled and no OIDC issuer each make a browser UI impossible, and both are
    the default — an operator has to be told."""
    notes = (TEMPLATES_DIR / "NOTES.txt").read_text()
    assert "CORS is disabled" in notes
    assert "no OIDC issuer" in notes


def test_the_nodepool_script_matches_the_prod_config() -> None:
    """The heavy-workload label and taint are one decision written in two places; if they
    disagree, heavy sandboxes either stop being segregated or stop scheduling, and
    neither failure is loud."""
    script = (REPO_ROOT / "deploy" / "azure" / "provision-nodepools.sh").read_text()
    prod = yaml.safe_load((REPO_ROOT / "config" / "settings" / "aks-prod.yaml").read_text())

    selector = prod["provisioner"]["heavy_node_selector"]
    assert selector == {"kubesandbox.io/workload-class": "heavy"}
    assert "kubesandbox.io/workload-class=heavy" in script

    toleration = prod["provisioner"]["heavy_tolerations"][0]
    assert f'{toleration["key"]}={toleration["value"]}:{toleration["effect"]}' in script


def test_no_replace_me_remains_in_the_prod_provisioner_config() -> None:
    """Phase 9's job was to make these real. Other REPLACE_ME values (DSNs, vault URLs,
    the ACR host) are legitimately per-deployment and stay."""
    prod = yaml.safe_load((REPO_ROOT / "config" / "settings" / "aks-prod.yaml").read_text())
    assert "REPLACE_ME" not in str(prod["provisioner"])
