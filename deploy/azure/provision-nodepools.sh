#!/usr/bin/env bash
#
# Provisions the two AKS node pools doc §6 Layer 2 and doc §4.3 require, and which the
# roadmap's Phase 9 explicitly scopes as "cluster/infra-level, outside this repo's code,
# but referenced by aks-prod.yaml":
#
#   1. A gVisor/Kata pool — kernel-level isolation for sandbox pods. Without it,
#      `provisioner.runtime_class: gvisor` makes every sandbox pod unschedulable, because
#      the RuntimeClass names a containerd handler no node has.
#   2. A segregated `heavy` pool — so heavy workloads "must not starve light ones"
#      (doc §4.3). Labelled and tainted; the control plane targets it via
#      `provisioner.heavy_node_selector` / `heavy_tolerations`.
#
# Idempotent: every `az aks nodepool add` is guarded by a `show` first, so re-running
# after a partial failure is safe.
#
# This script is NOT run by anything automatically. Node pools are expensive, slow to
# create, and cluster-lifecycle concerns — running them from a deploy pipeline is how you
# get a surprise bill. Run it once, deliberately, per cluster.
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:?set RESOURCE_GROUP}"
CLUSTER_NAME="${CLUSTER_NAME:?set CLUSTER_NAME}"

# --- gVisor / sandboxed-containers pool ----------------------------------------------
# Azure exposes gVisor through the "KataVmIsolatedContainers"/Pod Sandboxing add-on
# rather than as a raw runsc install, and it constrains the VM sizes and OS SKU that can
# host it. The two invariants that matter and are easy to get wrong:
#   * the VM size must support nested virtualization (Dv5/Dsv5 and similar) — a size
#     without it fails at pool creation with an unhelpful error;
#   * the workload runtime is a pool-level property, not a pod-level one, which is
#     exactly why this is a separate pool rather than a flag on the default one.
ISOLATION_POOL="${ISOLATION_POOL:-gvisor}"
ISOLATION_VM_SIZE="${ISOLATION_VM_SIZE:-Standard_D4s_v5}"
ISOLATION_MIN="${ISOLATION_MIN:-1}"
ISOLATION_MAX="${ISOLATION_MAX:-10}"

# --- heavy-workload pool --------------------------------------------------------------
# The label and taint below MUST match config/settings/aks-prod.yaml's
# `provisioner.heavy_node_selector` and `provisioner.heavy_tolerations`. They are two
# halves of one decision: the taint keeps everything *else* off these nodes, and the
# nodeSelector + toleration is how a heavy sandbox pod gets on. Change one and heavy
# sandboxes silently stop being segregated (or stop scheduling entirely).
HEAVY_POOL="${HEAVY_POOL:-heavy}"
HEAVY_VM_SIZE="${HEAVY_VM_SIZE:-Standard_D8s_v5}"
HEAVY_MIN="${HEAVY_MIN:-0}"
HEAVY_MAX="${HEAVY_MAX:-6}"
HEAVY_LABEL="kubesandbox.io/workload-class=heavy"
HEAVY_TAINT="kubesandbox.io/heavy=true:NoSchedule"

pool_exists() {
  az aks nodepool show \
    --resource-group "$RESOURCE_GROUP" \
    --cluster-name "$CLUSTER_NAME" \
    --name "$1" >/dev/null 2>&1
}

echo "==> gVisor / sandboxed-containers pool: $ISOLATION_POOL"
if pool_exists "$ISOLATION_POOL"; then
  echo "    already exists, skipping"
else
  az aks nodepool add \
    --resource-group "$RESOURCE_GROUP" \
    --cluster-name "$CLUSTER_NAME" \
    --name "$ISOLATION_POOL" \
    --node-vm-size "$ISOLATION_VM_SIZE" \
    --os-sku AzureLinux \
    --workload-runtime KataMshvVmIsolation \
    --enable-cluster-autoscaler \
    --min-count "$ISOLATION_MIN" \
    --max-count "$ISOLATION_MAX" \
    --labels kubesandbox.io/isolation=gvisor \
    --mode User
  # Min-count 1, not 0: this pool hosts every sandbox, so scaling it to zero means the
  # first request after an idle period waits for a node to be provisioned — minutes,
  # against a doc §5.1 call that is supposed to be bounded by a 60s wall-clock cap.
fi

echo "==> heavy-workload pool: $HEAVY_POOL"
if pool_exists "$HEAVY_POOL"; then
  echo "    already exists, skipping"
else
  az aks nodepool add \
    --resource-group "$RESOURCE_GROUP" \
    --cluster-name "$CLUSTER_NAME" \
    --name "$HEAVY_POOL" \
    --node-vm-size "$HEAVY_VM_SIZE" \
    --os-sku AzureLinux \
    --workload-runtime KataMshvVmIsolation \
    --enable-cluster-autoscaler \
    --min-count "$HEAVY_MIN" \
    --max-count "$HEAVY_MAX" \
    --labels "$HEAVY_LABEL" \
    --node-taints "$HEAVY_TAINT" \
    --mode User
  # Min-count 0 here, unlike the pool above: heavy sandboxes are rare and expensive, and
  # a cold start of several minutes is an acceptable trade for not paying for an idle
  # D8s. The taint is what makes scaling to zero safe — nothing else will ever land here
  # and hold the pool up.
fi

echo
echo "==> Verify"
az aks nodepool list \
  --resource-group "$RESOURCE_GROUP" \
  --cluster-name "$CLUSTER_NAME" \
  --output table

cat <<'NEXT'

Next steps (neither is done by this script):

  1. Apply the sandbox primitives, which include the `gvisor` RuntimeClass object that
     makes the pool above actually reachable from a pod spec:

       kubectl apply -k deploy/overlays/aks-prod

  2. Confirm config/settings/aks-prod.yaml matches the label and taint set here:

       provisioner:
         heavy_node_selector:
           kubesandbox.io/workload-class: "heavy"
         heavy_tolerations:
           - key: "kubesandbox.io/heavy"
             operator: "Equal"
             value: "true"
             effect: "NoSchedule"

     These are the same decision expressed twice. If they disagree, heavy sandboxes
     either stop being segregated or stop scheduling — and neither failure is loud.
NEXT
