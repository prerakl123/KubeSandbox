# KubeSandbox — Deployment Guide

Concrete, honest steps to run this repo's code somewhere real — `local` (already
covered in depth by `README.md`; summarized here for completeness), Azure/AKS (the
one cloud this repo actually implements, per `docs/ARCHITECTURE_AND_PLAN.md` §9), and
a generic/self-hosted Kubernetes cluster (EKS, GKE, bare-metal, or any other cluster —
what works as-is versus what needs code that doesn't exist yet). Where something is
genuinely missing rather than just unconfigured, this doc says so plainly instead of
implying it's a `kubectl apply` away.

---

## 1. What actually exists to deploy

| Piece | Status |
|---|---|
| Control-plane FastAPI app (`app/main.py`) | Real, containerizable (`deploy/dockerfiles/Dockerfile.control-plane`) |
| Reconciler worker (`app/reconciler/loop.py`) | Real, standalone-runnable (`uv run python -m app.reconciler.loop`) — needs its own process/pod, separate from the API |
| `DockerProvisioner` | Real — `local` only |
| `KubernetesProvisioner` | Real — cloud-agnostic, talks to any K8s API server via `kubernetes_asyncio`; not Azure-specific |
| Sandbox-primitive Kustomize manifests (`deploy/manifests/base`, `deploy/overlays/{local,aks-prod}`) | Real, cluster-agnostic except the aks-prod overlay's egress CIDRs |
| **Control-plane Helm chart** (`deploy/helm/kubesandbox/`) | **Does not exist** — directory is empty, roadmap Phase 9. See §4 for a stopgap. |
| `AzureKeyVaultSecretsProvider`, `AzureBlobStorageProvider`, `ACRRegistryProvider` | Real, Azure-specific |
| `MinIOStorageProvider`, `LocalImageStore` | Real, cloud-agnostic (self-hosted stand-ins) |
| AWS/GCP `SecretsProvider`/`ObjectStorageProvider`/`ImageRegistryProvider` | **Explicit stubs** — `raise NotImplementedError(...)` on first call, by design (doc §9) |
| Prometheus `/metrics`, OpenTelemetry tracing | **Do not exist** — roadmap Phase 9 |
| Billing, quota enforcement, rate limiting, API-key issuance endpoints | **Do not exist** — roadmap Phase 8 / cross-cutting |

This shapes every section below: `local` and `aks-prod` are the two environments the
codebase is actually designed around (`docs/ARCHITECTURE_AND_PLAN.md` §7 is explicit
that these are the *only* two — no separate "dev" profile). A third,
generic-Kubernetes path is possible today by reusing the same self-hosted building
blocks `local` already uses (MinIO, a self-hosted registry, K8s Secrets directly)
instead of Azure's managed services — not a new code path, just a different config
selection.

---

## 2. Local (Docker Compose) — summary

Full walkthrough: `README.md`. In short:

```bash
uv sync
docker compose up -d          # Postgres, Redis, MinIO, local registry
# build each source.type: image component's golden image manually (README §"One-time setup")
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

`SANDBOX_BACKEND` defaults to `docker`; `KUBESANDBOX_PROVISIONER__BACKEND=kubernetes`
switches to a local `kind` cluster instead (also in the README) — useful for
K8s-spec-accurate testing before touching a real cloud.

---

## 3. Azure / AKS production deployment

### 3.1 Azure resources to provision

None of this is scripted in-repo (no Terraform/Bicep exists here) — illustrative `az`
CLI commands, not a turnkey script:

```bash
# Resource group
az group create -n kubesandbox-prod -l eastus

# AKS cluster — Azure CNI + a network-policy engine is required, or doc §12's
# default-deny NetworkPolicy is silently unenforced (kubenet has no netpol support at all)
az aks create -n kubesandbox-prod -g kubesandbox-prod \
  --network-plugin azure --network-policy azure \
  --node-count 3 --node-vm-size Standard_D4s_v5 \
  --enable-managed-identity

# A second, tainted node pool for `heavy` weight-class segregation (doc §4.3) —
# matches config/settings/aks-prod.yaml's heavy_node_selector/heavy_tolerations
az aks nodepool add --cluster-name kubesandbox-prod -g kubesandbox-prod \
  --name heavy --node-vm-size Standard_D8s_v5 --node-count 2 \
  --labels kubesandbox.io/workload-class=heavy \
  --node-taints kubesandbox.io/heavy=true:NoSchedule

# ACR (image_registry.provider: acr)
az acr create -n kubesandboxprod -g kubesandbox-prod --sku Standard
az aks update -n kubesandbox-prod -g kubesandbox-prod --attach-acr kubesandboxprod

# Managed Postgres (database.dsn)
az postgres flexible-server create -n kubesandbox-pg -g kubesandbox-prod \
  --sku-name Standard_D2ds_v5 --tier GeneralPurpose

# Redis (redis.url) — Azure Cache for Redis, or self-hosted in-cluster if cost matters more than SLA
az redis create -n kubesandbox-redis -g kubesandbox-prod --sku Standard --vm-size c1

# Storage account for Blob (object_storage.provider: azure_blob)
az storage account create -n kubesandboxprod -g kubesandbox-prod --sku Standard_LRS
az storage container create --account-name kubesandboxprod -n kubesandbox

# Key Vault (secrets.provider: azure_keyvault) + CSI driver add-on
az keyvault create -n kubesandbox-prod -g kubesandbox-prod
az aks enable-addons -n kubesandbox-prod -g kubesandbox-prod --addons azure-keyvault-secrets-provider
```

`gVisor`/Kata node pool: unlike GKE's Sandbox GA feature, AKS has no first-class
"managed gVisor node pool" option as of this repo's own research pass — getting
`runtimeClassName: gvisor` (already wired in `KubernetesProvisioner`, doc §6 Layer 2)
to actually take effect needs a node pool whose nodes have the `runsc` containerd
shim installed, which today means either a custom node image/DaemSet-based installer
or Kata Containers via a confidential-computing node pool. **Verify current AKS
capability directly against Microsoft's docs before committing to an approach** — this
is genuine infrastructure work outside this repo's code, exactly the gap
`docs/TASK_CHECKLIST.md`'s Phase 9 entry already flags, not something a manifest here
can paper over.

### 3.2 Build & push images

```bash
az acr login -n kubesandboxprod

# Control plane
docker build -t kubesandboxprod.azurecr.io/kubesandbox-control-plane:latest \
  -f deploy/dockerfiles/Dockerfile.control-plane .
docker push kubesandboxprod.azurecr.io/kubesandbox-control-plane:latest

# Golden images — python/node/go/base/git predate BuildManager and stay on this
# manual path on purpose (see README's "One-time setup"); jq/ripgrep/httpie/demo-echo
# go through POST /v1/components/{name}/build once the control plane is already up
# and pointed at image_registry.provider: acr
for component in components/languages/python components/languages/node components/languages/go components/base; do
  docker build -t kubesandboxprod.azurecr.io/kubesandbox/$(basename "$component"):<version> "$component"
  docker push kubesandboxprod.azurecr.io/kubesandbox/$(basename "$component"):<version>
done
```

### 3.3 Configure `config/settings/aks-prod.yaml`

Replace every `REPLACE_ME` with the real values from §3.1 — `database.dsn` (via env
var/Key Vault, never committed), `redis.url`, `image_registry.endpoint`
(`kubesandboxprod.azurecr.io`), `secrets.vault_url`, and
`provisioner.heavy_node_selector`/`heavy_tolerations` (must match the node pool's
actual labels/taints from §3.1). Secrets themselves (DB password, etc.) come from Key
Vault via the CSI driver mounted into the control-plane pod, never from the YAML file
directly (doc §6 Layer 6) — see §3.5's Secret Provider Class example.

### 3.4 Apply the sandbox-primitive Kustomize overlay

```bash
kubectl apply -k deploy/overlays/aks-prod
```

This creates `kubesandbox-system` (the control plane's own namespace — distinct from
the per-sandbox namespaces `KubernetesProvisioner` creates dynamically), the
`kubesandbox-controller` ClusterRole/Binding, default-deny NetworkPolicy + the prod
egress allowlist, ResourceQuota, LimitRange, and the `gvisor` RuntimeClass object.
**Before applying**, fill in `deploy/overlays/aks-prod/networkpolicy-allow.yaml`'s
`REPLACE_ME` CIDR block with the real VNet subnet(s) for Key Vault/ACR/Postgres/Redis
private endpoints — NetworkPolicy matches on IP/namespace/pod selectors only, never
hostnames, so this can't be expressed any other way.

### 3.5 Deploy the control plane itself (no Helm chart yet — stopgap manifests)

`deploy/helm/kubesandbox/` is empty (Phase 9 roadmap item). Until that chart exists,
here is a minimal, illustrative raw manifest — adjust resource requests, replica
count, and the `ServiceAccount`/CSI `SecretProviderClass` names to your actual Key
Vault setup before applying for real:

```yaml
# deploy/manifests/control-plane-stopgap.yaml (not currently in this repo — write it
# yourself from this example, or use it as the seed for the eventual Helm chart)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kubesandbox-api
  namespace: kubesandbox-system
spec:
  replicas: 2                      # stateless — any replica serves any session, doc §2
  selector: { matchLabels: { app: kubesandbox-api } }
  template:
    metadata: { labels: { app: kubesandbox-api } }
    spec:
      serviceAccountName: kubesandbox-controller
      containers:
        - name: api
          image: kubesandboxprod.azurecr.io/kubesandbox-control-plane:latest
          command: ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
          env:
            - { name: KUBESANDBOX_APP_ENV, value: "aks-prod" }
          ports: [{ containerPort: 8000 }]
          volumeMounts:
            - { name: kv-secrets, mountPath: "/mnt/secrets", readOnly: true }
      volumes:
        - name: kv-secrets
          csi:
            driver: secrets-store.csi.k8s.io
            readOnly: true
            volumeAttributes: { secretProviderClass: "kubesandbox-kv" }
---
apiVersion: v1
kind: Service
metadata: { name: kubesandbox-api, namespace: kubesandbox-system }
spec:
  selector: { app: kubesandbox-api }
  ports: [{ port: 80, targetPort: 8000 }]
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kubesandbox-reconciler
  namespace: kubesandbox-system
spec:
  replicas: 1                       # exactly one — the reconciler is not designed to run concurrently with itself
  selector: { matchLabels: { app: kubesandbox-reconciler } }
  template:
    metadata: { labels: { app: kubesandbox-reconciler } }
    spec:
      serviceAccountName: kubesandbox-controller
      containers:
        - name: reconciler
          image: kubesandboxprod.azurecr.io/kubesandbox-control-plane:latest
          command: ["uv", "run", "python", "-m", "app.reconciler.loop"]
          env:
            - { name: KUBESANDBOX_APP_ENV, value: "aks-prod" }
```

Put an Ingress/Gateway in front of `kubesandbox-api`'s Service per your cluster's own
ingress controller — nothing in this repo prescribes one.

### 3.6 Run migrations

```bash
# From any pod/job with network access to the managed Postgres and this image
kubectl run kubesandbox-migrate --rm -it --restart=Never \
  --image=kubesandboxprod.azurecr.io/kubesandbox-control-plane:latest \
  --env="KUBESANDBOX_APP_ENV=aks-prod" \
  -- uv run alembic upgrade head
```

### 3.7 Verify

```bash
kubectl -n kubesandbox-system get pods
kubectl -n kubesandbox-system logs deploy/kubesandbox-api
curl https://<your-ingress-host>/healthz
curl https://<your-ingress-host>/readyz
```

---

## 4. Generic / self-hosted Kubernetes (EKS, GKE, bare-metal, a bigger kind cluster)

`KubernetesProvisioner` itself has no Azure dependency — it's plain
`kubernetes_asyncio` against whatever API server `KUBECONFIG` (or in-cluster config)
points at. What changes moving off Azure is entirely the **CloudProvider layer**
(doc §9), not the sandbox provisioner:

| Concern | Works today via | Needs new code |
|---|---|---|
| Secrets | K8s Secrets directly (skip `secrets.provider` entirely, mount as env/volumes the normal K8s way) | `AWSSecretsManagerProvider`/`GCPSecretManagerProvider` if you want the app to fetch secrets itself rather than via the platform |
| Object storage | `object_storage.provider: minio` — self-hosted MinIO in-cluster, or any real S3-compatible endpoint (many providers' object storage speaks the S3 API) | Real `AWSObjectStorageProvider`/`GCPObjectStorageProvider` (`app/cloud/storage.py` — currently `raise NotImplementedError`) |
| Image registry | `image_registry.provider: local` pointed at any registry that speaks the Docker Registry v2 API (self-hosted `registry:2`, GitHub Container Registry, Docker Hub) | Real `AWSImageRegistryProvider`(ECR)/`GCPImageRegistryProvider`(Artifact Registry) (`app/cloud/registry.py` — currently stubs) for a cloud-native OAuth2 push flow like `ACRRegistryProvider` already implements |
| NetworkPolicy enforcement | Whatever the cluster's CNI provides — confirmed live on `kind`'s default CNI (Phase 3) and requires Azure CNI+`--network-policy azure` on AKS; **Calico, Cilium, or the EKS/GKE-native equivalents all need the same explicit enable step** — a cluster with no NetworkPolicy-capable CNI silently does NOT enforce doc §6 Layer 3's default-deny | — |
| Heavy-node segregation | `provisioner.heavy_node_selector`/`heavy_tolerations` — plain K8s primitives, works on any cluster once a real tainted node pool exists | — |
| gVisor/Kata | `RuntimeClass` object + `runtimeClassName` wiring, cluster-agnostic | A node pool with the actual `runsc`/Kata shim installed — GKE Sandbox is the most turnkey option if that specific cloud is in play; elsewhere this is manual node provisioning |

Implementing a real AWS or GCP provider follows the exact pattern
`ACRRegistryProvider`/`AzureBlobStorageProvider` already establish in
`app/cloud/{registry,storage}.py` — same `Protocol`, different SDK underneath
(`boto3`/`aioboto3` for AWS, `google-cloud-storage`/`google-cloud-artifact-registry`
for GCP). Nothing else in the codebase would need to change; `app/core/bootstrap.py`
already dispatches on `settings.*.provider` and just needs the new class registered
there once it exists.

Steps 3.2–3.7 above otherwise carry over unchanged — swap ACR/Key Vault/Blob commands
for your platform's equivalents (or the self-hosted stand-ins from the table above),
point `KUBESANDBOX_APP_ENV=aks-prod`'s YAML at real values (or add a genuinely new
profile — the two-environment restriction in doc §7 is a stated project decision, not
a technical one; nothing prevents a third `config/settings/<name>.yaml` file and
`KUBESANDBOX_APP_ENV=<name>` if that restriction is deliberately relaxed).

---

## 5. Logging & observability

### 5.1 What exists today

`app/core/logging.py` configures `structlog`: **structured JSON to stdout** in every
non-debug environment (pretty console output only when `debug: true`, i.e. `local`),
with `TimeStamper`, log level, and exception formatting baked into every event.
That's the entire story right now — no correlation/request-ID middleware, no
Prometheus client, no OpenTelemetry SDK, no Application Insights/Azure Monitor
integration anywhere in the dependency tree (`pyproject.toml` has `structlog` and
nothing else observability-related). `AuditLog` (the doc §10.1 audit trail table)
exists in the schema but nothing writes to it yet — a cross-cutting gap, not
Phase-7-specific.

There are genuinely three distinct kinds of "logs" in this system, each flowing
differently:

1. **The control plane's own operational logs** (`app/main.py`, `SandboxService`,
   `PoolManager`, the reconciler, etc.) — structlog events to stdout, exactly as
   described above.
2. **Platform/infrastructure logs** — kubelet, the container runtime, AKS/EKS/GKE's
   own control-plane logs. Entirely outside this repo's code; each platform exposes
   these differently (see §5.3).
3. **Sandboxed user-code run output** — a batch run's `stdout`/`stderr` (doc §5.1).
   This is **not** logged via structlog at all — it's domain data, captured by the
   provisioner's `exec_batch()`, returned in full (bounded by the component's own
   `outputBytes` limit, default 5 MB) in the `BatchRunResult` HTTP response, and
   separately persisted to Postgres as `runs.stdout_excerpt`/`stderr_excerpt`,
   **truncated to 10,000 characters** (`app/services/sandbox_service.py`). Doc §10.1
   describes an object-storage overflow path for output beyond that ("run logs
   (overflow)") — **this is not implemented**; anything past 10k characters is
   simply not retained anywhere once the HTTP response that returned it is gone.

### 5.2 How logs actually flow per environment (today, unmodified)

- **`local`**: `uv run uvicorn ...` prints structlog's pretty console output directly
  to your terminal. Nothing is persisted beyond your shell's scrollback unless you
  redirect it (`... 2>&1 | tee kubesandbox.log`).
- **`aks-prod` (or any Kubernetes)**: every container's stdout/stderr — the control
  plane, the reconciler, and (rarely, since sandboxes don't log via structlog at all)
  a sandbox pod's own stderr if its process crashes before `exec_batch` even starts —
  is captured by the container runtime and written to node-local log files, readable
  via `kubectl logs <pod>` or your platform's own aggregated view (Azure Monitor
  Container Insights, EKS's default CloudWatch integration if enabled, GKE's built-in
  Cloud Logging). **No code in this repo does this shipping** — it's the platform's
  own logging pipeline, working purely because these processes write JSON lines to
  stdout, which every container runtime already captures by convention.

### 5.3 Feasibility of wiring up a specific sink

The structured-JSON-to-stdout design is deliberately sink-agnostic — every option
below is additive, not a rearchitecture, because none of them require changing what
gets logged, only where the JSON lines end up:

| Target | Feasibility | What it'd actually take |
|---|---|---|
| **Plain file** (`log.log`, rotated) | Trivial | Point `structlog.PrintLoggerFactory(file=...)` at an open file handle instead of `sys.stdout`, or just redirect the process's stdout at the OS/systemd level. No code change needed for the redirect case. |
| **Loki** (Grafana stack) | Easy | Nothing in-app — deploy Promtail/Grafana Agent as a DaemonSet reading container stdout (the standard Loki-on-Kubernetes pattern); JSON logs parse cleanly with Loki's `json` pipeline stage out of the box. |
| **ELK/Elasticsearch** | Easy | Same shape as Loki — Filebeat/Fluent Bit DaemonSet reading stdout, shipping to Elasticsearch. No app changes. |
| **Azure Monitor / Application Insights** | Moderate | Two real options: (a) **Container Insights** (AKS add-on) ships stdout to a Log Analytics workspace with zero app changes — good enough for log search/alerting, not distributed tracing; (b) real **Application Insights** integration (traces linked to requests, live metrics) needs the `azure-monitor-opentelemetry` SDK added as a dependency and an OpenTelemetry `LoggingHandler`/exporter wired into `app/core/logging.py` (or bridged from structlog's stdlib-`logging` integration, which structlog already supports) plus FastAPI/httpx auto-instrumentation for request-level traces. This is genuinely the same OpenTelemetry work `docs/ARCHITECTURE_AND_PLAN.md` §14/§20 Phase 9 already calls out ("OpenTelemetry traces spanning API → provisioner → pod") — Application Insights would just be the concrete exporter target once that SDK is added. |
| **AWS CloudWatch (EKS)** | Easy–Moderate | Fluent Bit (EKS's own default log router) ships stdout with no app changes for log search; CloudWatch *metrics*/X-Ray tracing would need the same OpenTelemetry work as above, exported via the AWS Distro for OpenTelemetry (ADOT) collector instead of Azure Monitor's exporter. |
| **GCP Cloud Logging (GKE)** | Easy | GKE ships container stdout to Cloud Logging automatically, no app changes; structured JSON fields are queryable as-is. Cloud Trace would again need OpenTelemetry instrumentation. |
| **Prometheus `/metrics`** | Not yet built | Roadmap Phase 9 item, no code exists (`sandboxes_active`, `provision_latency`, `run_duration`, `build_duration`, `pool_hit_rate`, quota/credit usage are the doc §14 metrics list) — adding `prometheus-client`, a metrics registry, and a `/metrics` route is the concrete first step; several counters (pool claims/replenishment, reconciler tick timings) already have natural instrumentation points in this phase's own code (`PoolManager`, `ReconcilerLoop.tick()`). |

**Bottom line**: log *shipping* to any of the above needs zero changes to this
codebase on Kubernetes — it's a platform-level DaemonSet/add-on decision, because
JSON-to-stdout is already the universal contract every one of these tools expects.
Real distributed *tracing* (spans across API → provisioner → sandbox) and
*metrics* are the two pieces that need actual code (an OpenTelemetry SDK + exporter,
and `prometheus-client` + a `/metrics` route respectively) — both are scoped,
well-understood additions, not open design questions, and both are already named
explicitly in the architecture doc's Phase 9 rather than being a new idea.

---

## 6. Known gaps (repeated from `docs/TASK_CHECKLIST.md` for deployment-planning convenience)

- No Helm chart for the control plane (§3.5's manifests are a stopgap, not this repo's
  intended final state).
- No Kaniko/K8s-Job build path — `BuildManager`'s `dockerfile`/`compose`/`pipeline`
  strategies only build against a local Docker daemon; running `BuildManager` itself
  inside a Kubernetes pod (no Docker socket available there by default) is
  unaddressed.
- No `/metrics`, no distributed tracing, no `AuditLog` writes yet.
- No billing/quota/rate-limiting enforcement (roadmap Phase 8 / cross-cutting).
- AWS/GCP `CloudProvider` implementations are stubs — see §4's table.
- Real gVisor/Kata execution has never been exercised against real infrastructure in
  this repo's own testing (kind has no such node); verify independently before
  relying on it in production.
