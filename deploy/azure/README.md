# Azure / AKS prerequisites

Everything here is **cluster and subscription level** — outside the control plane's own
code, but referenced by `config/settings/aks-prod.yaml` and by the Helm chart. The
roadmap scopes it this way deliberately (Phase 9's first two items are explicitly
"cluster/infra-level, outside this repo's code"), and none of it is run automatically:
node pools are expensive and slow to create, and a deploy pipeline that provisions
infrastructure as a side effect is how you get a surprise bill.

Run these once, in order, per cluster.

## 0. What you need first

- An AKS cluster and the `az` CLI logged in (`az login`, `az account set --subscription …`).
- The `aks-preview` extension for the Pod Sandboxing / Kata isolation flags:
  `az extension add --name aks-preview`.
- Nested-virtualization-capable VM sizes available in your region (Dsv5 family and
  similar). A size without nested virtualization fails pool creation with an unhelpful
  error.

## 1. Node pools

```bash
export RESOURCE_GROUP=kubesandbox-prod
export CLUSTER_NAME=kubesandbox-prod-aks
./provision-nodepools.sh
```

This creates two pools, and the reasons they're two rather than one are the whole point:

| Pool | Why it exists | Autoscale floor |
|---|---|---|
| `gvisor` | Doc §6 Layer 2's kernel isolation. `provisioner.runtime_class: gvisor` makes **every** sandbox pod unschedulable without a node whose containerd has the shim — the RuntimeClass object alone is inert. | **1** — this pool hosts every sandbox, and scaling to zero means the first request after an idle period waits minutes for a node, against a call that's supposed to be bounded by a 60s cap. |
| `heavy` | Doc §4.3's "heavy templates must not starve light ones". Labelled `kubesandbox.io/workload-class=heavy` and tainted `kubesandbox.io/heavy=true:NoSchedule`. | **0** — heavy sandboxes are rare and the VMs are expensive. The taint is what makes scaling to zero safe: nothing else ever lands here to hold the pool up. |

The label and taint must match `config/settings/aks-prod.yaml`'s
`provisioner.heavy_node_selector` / `heavy_tolerations` exactly. They're one decision
written in two places; if they disagree, heavy sandboxes either stop being segregated or
stop scheduling entirely, and **neither failure is loud**. The script prints the values
back at the end for exactly this reason.

## 2. Sandbox primitives (Kustomize, not Helm)

```bash
kubectl apply -k ../overlays/aks-prod
```

This is what creates the `gvisor` RuntimeClass object, the default-deny NetworkPolicy,
and the ResourceQuota/LimitRange. It's deliberately *not* in the Helm chart: doc §12
makes the egress allowlist "entirely a deployment-overlay concern", and splitting one
owner's decision across two templating systems is how those drift apart.

## 3. Registry access

Attach the ACR to the cluster's kubelet identity rather than copying registry
credentials into a Kubernetes Secret:

```bash
az aks update --resource-group "$RESOURCE_GROUP" --name "$CLUSTER_NAME" \
  --attach-acr <acr-name>
```

This is why the chart's `imagePullSecrets` defaults to empty.

## 4. Identity for Key Vault / Blob / ACR

`app/cloud/*.py` authenticates through `DefaultAzureCredential`, which in AKS resolves
to a workload identity. Create one, federate it with the chart's ServiceAccount, and
grant it what it needs:

```bash
# Workload identity + OIDC issuer on the cluster
az aks update --resource-group "$RESOURCE_GROUP" --name "$CLUSTER_NAME" \
  --enable-oidc-issuer --enable-workload-identity

az identity create --resource-group "$RESOURCE_GROUP" --name kubesandbox-control-plane
```

Then federate it with the release's ServiceAccount
(`serviceAccountName` from the chart, in the release namespace) and grant:

- **Key Vault** — `get` on secrets (`AzureKeyVaultSecretsProvider`, and the Secrets
  Store CSI driver if you use `secrets.keyVault.enabled`).
- **Blob Storage** — `Storage Blob Data Contributor` on the account
  (`AzureBlobStorageProvider`: run-log overflow, build cache, archived workspaces).
- **ACR** — `AcrPush` if the control plane itself pushes built images
  (`ACRRegistryProvider`).

Finally set the identity's client id in the chart:

```yaml
serviceAccount:
  annotations:
    azure.workload.identity/client-id: <identity client id>
```

## 5. Secrets

Two supported paths; pick one.

**An existing Secret** (simplest, no CSI driver needed):

```bash
kubectl create secret generic kubesandbox-secrets -n <release-namespace> \
  --from-literal=KUBESANDBOX_DATABASE__DSN='postgresql+asyncpg://…' \
  --from-literal=KUBESANDBOX_REDIS__URL='redis://…' \
  --from-literal=KUBESANDBOX_AUTH__JWT_SECRET="$(openssl rand -base64 48)"
```

Then `secrets.existingSecret: kubesandbox-secrets`.

**Key Vault CSI** (doc §7's stated approach): install the Secrets Store CSI driver and
its Azure provider, put the same three values in the vault, and set
`secrets.keyVault.enabled: true` with the vault name, tenant id, and identity client id.

The JWT secret must be **32+ bytes**: `Settings` refuses the committed placeholder and
anything shorter outside `app_env=local` (RFC 7518 §3.2's floor for HS256 — this key
mints session tokens, so a short one is brute-forcible offline).

## 6. Install the control plane

```bash
helm upgrade --install kubesandbox ../helm/kubesandbox \
  --namespace kubesandbox-system --create-namespace \
  --set image.repository=<acr>.azurecr.io/kubesandbox/control-plane \
  --set secrets.existingSecret=kubesandbox-secrets \
  --set config.cors.enabled=true \
  --set 'config.cors.allowOrigins[0]=https://<ui-host>' \
  --set config.auth.oidcIssuer='https://login.microsoftonline.com/<tid>/v2.0' \
  --set config.auth.oidcAudience='<api-app-client-id>' \
  --set config.auth.oidcClientId='<spa-client-id>'
```

The chart's `NOTES.txt` warns about anything still unconfigured after install —
including the two that block a UI outright (CORS disabled, no OIDC issuer).

## 7. Required kubelet setting: per-pod PID limit

**Not set by anything in this repo, and it needs to be.** Kubernetes has no pod-level PID
limit field — it is a kubelet setting, so it belongs to whoever provisions the node pool:

```bash
# In the node pool's kubelet config (AKS: --kubelet-config with a JSON file)
{ "podPidsLimit": 256 }
```

Without it, doc §6's fork-bomb protection exists only on the Docker backend. A fork bomb on
`aks-prod` is still bounded — the pod's memory limit stops it — but by OOM rather than by a
clean PID ceiling, with more collateral scheduling noise. `access.limits.processes` in a
component manifest (128 by default) is honored on Docker and silently ignored on
Kubernetes, which is the asymmetry this setting closes.

See `docs/SECURITY_HARDENING.md` item 6.

## What is still not automated

Honestly: the AAD app registrations. Creating the API and SPA app registrations,
configuring the SPA's redirect URIs, and exposing the API's scope are portal/`az ad`
steps that depend on choices this repo has no view into (single- vs multi-tenant, which
directory, whether the UI is a separate registration). The values they produce are the
three `config.auth.*` settings above.
