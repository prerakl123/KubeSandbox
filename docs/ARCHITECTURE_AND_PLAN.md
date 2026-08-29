# KubeSandbox — Architecture & Implementation Plan

> A configurable, plug-and-play control plane (FastAPI) that provisions isolated,
> per-request sandbox environments on Kubernetes (AKS prod) or Docker (local), streams
> their I/O to consumers, and exposes languages, databases, and tools as declaratively
> defined, versioned extensions.

---

## 1. Purpose, consumers & scope

**What it is.** A backend service that, on request, spins up a locked-down sandbox
(a pod or container) preloaded with a chosen set of components (languages, databases,
tools), runs user code inside it under strict guardrails, and returns/streams its I/O
back to the caller. Sandboxes are created per request and can be ephemeral or persistent.

**Two consumers, two execution modes (both first-class, intentionally different):**

| Consumer | Mode | I/O shape | Transport | Auth |
|---|---|---|---|---|
| **Workflow-builder "code block"** (programmatic) | **Batch** | stdin supplied up front; result delivered as one bundle (stdout, stderr, exit code, variable dump) after completion — **no live streaming, no runtime stdin wait** | REST (blocking or poll-for-completion) | Service token / API key |
| **Standalone users** (humans) | **Interactive** | live PTY: real-time stdout/stderr, live stdin, resize/signals | WebSocket | OIDC session / user token |

This split is deliberate, not a limitation: a workflow step is deterministic and
unattended, so it gets a clean single result object; a human at a terminal gets a real
shell. See §5 for the full contract.

**Design tenets:**

1. **Dual I/O, cleanly separated** — batch (bundled result) for workflows; interactive
   PTY (live) for humans. No hybrid live-streaming batch mode.
2. **Configurable lifecycle** — ephemeral by default; persistent workspaces per env/tier,
   with generous, admin-tunable quotas and retention (§10.2).
3. **Configurable isolation** — hardened containers everywhere; gVisor/Kata in `aks-prod`.
4. **Declarative extensions** — YAML component manifests + optional Python hooks.
5. **No live build/install in the request path** — every component is a pre-baked
   "golden image"; pooling/segregation absorb load, not just-in-time builds (§4.3, §8).
6. **Azure-first, cloud-pluggable** — Azure implemented now; AWS/GCP are explicit
   "Coming Soon" stubs behind the same interfaces (§9.1).
7. **No collaboration in v1** — one viewer per interactive sandbox; no multiplexed PTY.

**Explicit non-goals (v1):** general-purpose long-running app hosting, GPU workloads,
arbitrary inbound networking to sandboxes, giving any form of root/admin, multi-user
collaboration on one sandbox, live payment-gateway integration (billing computes and
records cost; collecting money is a stub, see §13).

---

## 2. High-level architecture

```
                    ┌────────────────────────────────────────────────────┐
   Workflow app ───▶│                 CONTROL PLANE (FastAPI)            │
   Standalone UI ──▶│                                                    │
                    │  API (REST + WS)                                   │
                    │  ├─ AuthN/Z            ├─ Component Registry       │
                    │  ├─ Sandbox Service    ├─ Template/Blueprint svc   │
                    │  ├─ Pool Manager       ├─ Build Manager            │
                    │  ├─ Entitlement Svc    ├─ Billing/Costing Svc      │
                    │  └─ Quota/RateLimit    └─ Reconciler (TTL/cleanup) │
                    │                                                    │
                    │  Provisioner ──┬── KubernetesProvisioner (aks-prod)│
                    │                └── DockerProvisioner (local)       │
                    │  CloudProvider ─┬── Azure (real)                   │
                    │                 └── AWS / GCP ("Coming Soon")      │
                    └───────┬───────────────┬───────────────┬────────────┘
                            │               │               │
             ┌──────────────▼───┐   ┌───────▼──────┐  ┌─────▼───────────┐
             │ PostgreSQL       │   │ Redis        │  │ Object store    │
             │ (metadata, audit,│   │ (sessions,   │  │ (logs, artifacts│
             │  billing, quotas)│   │  pool state, │  │  build cache,   │
             │                  │   │  rate limit) │  │  workspace bkp) │
             └──────────────────┘   └──────────────┘  └─────────────────┘
                            │
             ┌──────────────▼───────────────────── DATA PLANE ────────────────┐
             │  Kubernetes (aks-prod)  or  Docker + optional kind (local)     │
             │   namespace-per-sandbox (or per-tenant)                        │
             │   ┌─────────────── Sandbox Pod/Container ──────┐               │
             │   │ non-root uid 10001, readOnlyRootFS,        │               │
             │   │ dropped caps, seccomp, (gVisor in prod)    │               │
             │   │  ┌──────────┐ ┌──────────┐ ┌───────────┐   │               │
             │   │  │ main     │ │ postgres │ │ tool      │   │               │
             │   │  │ (langs)  │ │ sidecar  │ │ sidecars  │   │               │
             │   │  └──────────┘ └──────────┘ └───────────┘   │               │
             │   │  emptyDir /workspace (or PVC if persistent)│               │
             │   └────────────────────────────────────────────┘               │
             │   + NetworkPolicy (owned by deploy overlay), Quota, LimitRange │
             │   + Pool: idle claimable pods, keyed by (image digest, weight) │
             └────────────────────────────────────────────────────────────────┘
```

**Control plane / data plane split.** The FastAPI service is stateless and horizontally
scalable; all durable state lives in Postgres/Redis/object-store. Sandboxes live in the
data plane. Interactive `exec`/`attach` streams connect *directly* from a control-plane
replica to the pod via the K8s streaming API, so **any replica can serve any session** —
no sticky sessions or cross-replica stream fan-out needed.

---

## 3. The extension / component system (the heart)

Everything pluggable — languages, databases, tools, services, base images, even build
strategies — is modeled as a **Component**: a versioned YAML manifest (+ optional Python
hook module). Components are composed into **SandboxTemplates**. This is the
"terraform-style scripting" layer: declarative, versioned, environment-aware, rendered
into pod/container specs by the backend.

### 3.1 Component categories

`language` · `database` · `tool` · `service` · `runtime/base` · `build-strategy`

Beyond Python/JS + SQL/PostgreSQL, the registry is designed to hold:
- **Languages/CLIs:** Python, Node.js, Deno, Bun, Go, Rust, Java/JDK, .NET, Ruby, PHP,
  Bash/POSIX, Lua, R, Julia, TypeScript.
- **Databases/stores:** SQLite, PostgreSQL, MySQL/MariaDB, Redis, MongoDB, DuckDB.
- **Tools:** git, curl/httpie, jq, make, package managers, linters/formatters, ripgrep.
- **Services:** message brokers (Redis/RabbitMQ), object-store emulator (MinIO),
  headless browser (Playwright), Jupyter kernel server.
- **Build strategies:** `image`, `dockerfile`, `compose`, `pipeline`, `helm` (see §8).

### 3.2 Component manifest schema (example: a language)

```yaml
apiVersion: kubesandbox.io/v1
kind: Component
metadata:
  name: python
  version: "3.12.4"          # semver; multiple versions coexist in the registry
  category: language
  displayName: "Python 3.12"
  description: "CPython interpreter with pip"
spec:
  source:                    # how the golden image is produced (see §8)
    type: image              # image | dockerfile | compose | pipeline | helm
    image: { repository: kubesandbox/python, tag: "3.12.4-slim" }

  provides:                  # the CLI/language surface exposed to the user
    languageId: python
    commands: ["python", "python3", "pip"]
    fileExtensions: [".py"]
    versionCommand: "python --version"
    defaultRun: "python {file}"
    batchRunner:              # contract for bundled batch execution (see §5.3)
      entrypoint: "/opt/kubesandbox/runners/python_runner.py"
      supportsVariableDump: true
      stdinMode: upfront       # upfront = all stdin fed then EOF; no live wait ever

  runtime:                   # how it lands in the sandbox pod/container
    kind: mainTool           # mainTool | sidecar | init | ephemeral
    weightClass: light       # light | standard | heavy — drives pooling/segregation
    resources:
      requests: { cpu: "100m", memory: "128Mi" }
      limits:   { cpu: "1",    memory: "512Mi" }
    env: []
    volumeMounts: [{ name: workspace, mountPath: /workspace }]

  access:                    # guardrails (see §16)
    network: { egress: intent-only }   # a hint for the deploy overlay, not enforcement
    packages:
      manager: pip
      install: { enabled: true, source: "mirror", mirror: "https://pypi.internal/simple",
                 denylist: [], allowlist: [] }
    filesystem: { workdir: /workspace, writablePaths: ["/workspace","/tmp"],
                  readOnlyRootFilesystem: true }
    limits: { processes: 128, outputBytes: 5_000_000, wallClockSeconds: 60 }

  requires: []               # dependency on other components (by name@range)

  hooks:                     # OPTIONAL Python escape hatch
    module: "components.languages.python.hooks"   # implements provision/validate/teardown

  compatibility:
    environments: ["local", "aks-prod"]
    runtimeClass: { aks-prod: "gvisor" }
```

### 3.3 Component manifest schema (example: a database add-on)

```yaml
apiVersion: kubesandbox.io/v1
kind: Component
metadata: { name: postgresql, version: "16", category: database }
spec:
  source: { type: image, image: { repository: postgres, tag: "16-alpine" } }
  runtime:
    kind: sidecar
    weightClass: standard
    ports: [{ name: pg, containerPort: 5432 }]
    volumeMounts: [{ name: pgdata, mountPath: /var/lib/postgresql/data }]
    healthCheck: { exec: ["pg_isready","-U","sandbox"] }
    resources: { requests: {cpu:"100m",memory:"128Mi"}, limits: {cpu:"500m",memory:"256Mi"} }
  provides:
    commands: ["psql"]
    service: { protocol: postgres, port: 5432, dsnEnv: "DATABASE_URL" }
  access:
    database:                # DB-level ruleset (enforced via roles/grants, see §16)
      superuser: false
      role: sandbox_user
      grants: ["CONNECT","CREATE","TEMPORARY"]
      forbidden: ["SUPERUSER","CREATEROLE","REPLICATION","COPY ... TO/FROM PROGRAM"]
      limits: { maxConnections: 10, statementTimeout: "30s", maxDbSizeMB: 512 }
    network: { reachableFrom: same-pod-only }
```

### 3.4 SandboxTemplate (blueprint = composition of components)

```yaml
apiVersion: kubesandbox.io/v1
kind: SandboxTemplate
metadata: { name: python-postgres-lab, version: "1.0" }
spec:
  base: { ref: "base@1.0" }             # non-root base image (uid 10001)
  components:
    - ref: "python@3.12.4"
    - ref: "postgresql@16"
    - ref: "git@latest"
  weightClass: standard                  # overridable; else derived from components
  workspace: { persistent: false, sizeMB: 1024 }
  resources: { cpu: "2", memory: "2Gi", ephemeralStorageMB: 2048 }
  ttl: { idle: "15m", max: "2h" }
  overrides: {}                          # per-env resource/ttl overrides
```

### 3.5 Registry, versioning & hook contract

- **Registry** = manifests stored on disk under `components/` (source of truth in git),
  loaded and validated at startup and on reload, then indexed into Postgres for querying.
  Each `name@version` is immutable once published; templates pin exact versions (or ranges
  resolved at create-time).
- **Validation:** every manifest is validated against a JSON Schema (`schemas/`) before
  it can be registered. Invalid manifests fail loudly at CI and at load.
- **Python hook interface** (optional per component):

```python
class ComponentHook(Protocol):
    async def validate(self, ctx: BuildContext) -> None: ...       # publish-time checks
    async def mutate_pod_spec(self, spec: PodSpec, ctx: RenderContext) -> PodSpec: ...
    async def on_provision(self, sb: SandboxHandle, ctx: RenderContext) -> None: ...  # e.g. create DB role
    async def on_teardown(self, sb: SandboxHandle) -> None: ...
```

Hooks are the escape hatch for logic that can't be expressed declaratively (e.g. creating
a scoped Postgres role after the sidecar is healthy). Declarative-first; hooks only when needed.

### 3.6 Catalog curation & entitlements

Admins see and manage the **entire** registry (every component/template, every version).
Regular users/tenants see only what an admin has **entitled** them to — the extension
list a user is offered to pick from is a curated subset, not the raw registry.

- `component_entitlements(scope: tenant|user, scope_id, component_name, version_range,
  visible: bool)` — what a scope may *see and select*.
- `publish_grants(scope: tenant|user, scope_id, category, allowed: bool)` — whether a
  scope may *publish* its own (private, scope-owned) components/templates at all. Off by
  default; admins opt tenants/users in per category.
- Component/template listing endpoints always resolve through the caller's entitlements;
  admin endpoints bypass entitlement filtering entirely.
- Private, scope-owned components are namespaced (`tenant/<id>/<name>`) and never appear
  in another scope's catalog regardless of entitlements.

---

## 4. Sandbox lifecycle & orchestration

### 4.1 State machine

```
PENDING ─▶ PROVISIONING ─▶ READY ─▶ ACTIVE ⇄ IDLE ─▶ TERMINATING ─▶ TERMINATED
                 │                                          ▲
                 └──────────────▶ FAILED ──────────────────┘
```

- **create** → resolve template → merge component specs → check entitlements + quota +
  billing authorization (§13) → claim from pool or render pod/container spec fresh →
  provisioner applies → wait for readiness/health → run `on_provision` hooks → `READY`.
- **idle/max TTL** enforced by the **Reconciler** (background asyncio loop in a dedicated
  worker; upgradeable to a `kopf` operator in `aks-prod`). It also garbage-collects
  orphaned pods, reconciles desired vs. actual, reaps namespaces, and replenishes pools.
- **destroy** → run `on_teardown` hooks → finalize usage/billing record → delete
  namespace/pod → persist run records/logs.

### 4.2 Provisioner abstraction (the seam that makes local vs. AKS pluggable)

```python
class Provisioner(Protocol):
    async def acquire(self, spec: SandboxSpec) -> SandboxHandle: ...   # pool claim or fresh create
    async def exec_batch(self, h: SandboxHandle, cmd: BatchCommand) -> BatchRunResult: ...
    async def attach(self, h: SandboxHandle) -> PTYStream: ...          # interactive only
    async def status(self, h: SandboxHandle) -> SandboxStatus: ...
    async def put_files(self, h, files) -> None: ...
    async def recycle(self, h: SandboxHandle) -> None: ...              # wipe & return to pool
    async def destroy(self, h: SandboxHandle) -> None: ...
```

- `KubernetesProvisioner` — `kubernetes_asyncio` (or `lightkube`); exec/attach over the
  K8s streaming API. Used in `aks-prod`.
- `DockerProvisioner` — `aiodocker`; for `local` dev without a cluster. Same interface,
  same service-layer code above it.
- Selected by config (`SANDBOX_BACKEND=docker|kubernetes`), so the same service code runs
  in both environments.

### 4.3 Pod pooling & workload segregation

Golden images (§8) remove *build* latency from the request path, but at "hundreds of
concurrent interactive users" scale, scheduling + image-pull + container-start latency
still matters, and heavy workloads must not starve light ones. The **Pool Manager**
addresses both:

- **Weight classes** — `light | standard | heavy`, declared on a template (or derived
  from its components' `runtime.weightClass`). `heavy` templates are scheduled onto a
  segregated capacity pool: a dedicated AKS node pool + taints/tolerations in `aks-prod`,
  a separate resource budget/queue in `local`.
- **Warm pools** — a small number of idle, claimable pods are kept per
  `(golden-image-digest, weight-class)` key. Batch/workflow runs preferentially **claim**
  an idle pod (fast path) instead of creating one from scratch.
- **Claim → run → recycle-or-destroy** — after a batch run completes cleanly, the pod's
  `/workspace` is wiped and a health check re-run before it's returned to the pool
  (`recycle`); if the run errored, hit a resource limit, or the template is `heavy`, the
  pod is destroyed and the pool replenished instead, to avoid leaking dirty state.
- **Interactive sessions are never pooled after attach** — a session may originate from a
  pool claim (fast start) but is immediately **promoted** to a dedicated, non-recycled pod
  for the lifetime of that human's session, and destroyed (not recycled) on disconnect+TTL.
- Pool sizes, weight-class thresholds, and node-pool/queue mapping are environment config
  (`config/settings/{local,aks-prod}.yaml`), not hardcoded — an admin can tune them per
  observed load without a code change.

---

## 5. Execution & I/O model

### 5.1 Batch runs (workflow-builder path) — bundled result, no live streaming

- `POST /v1/execute` `{ language | template, code | command, stdin?, files?, timeout? }`
  → claims/creates an ephemeral sandbox, runs to completion (or timeout), tears down (or
  recycles), and returns **one bundled result**:

```json
{
  "run_id": "...",
  "exit_code": 0,
  "stdout": "...",
  "stderr": "...",
  "duration_ms": 842,
  "truncated": false,
  "variables": { "x": 42, "result": [1, 2, 3] }
}
```

- **stdin is entirely up front.** The full `stdin` string/bytes is written to the process
  and the pipe is then closed (EOF) *before or at* execution start. If the program blocks
  on a read past the provided input, it receives EOF immediately — it never waits on a
  live client, because there isn't one on this path.
- Default call **blocks** until the run finishes or hits its wall-clock cap (bounded by
  the same `wallClockSeconds` limit as everything else, so the HTTP call itself is
  time-bounded). An `?async=true` variant returns a `run_id` immediately for callers that
  prefer to poll `GET /v1/runs/{run_id}` until `status=completed`, at which point the same
  bundled result body is returned — there is still no incremental/streamed output on this
  path, only "done vs. not done yet".
- Supports an **idempotency key** so retries don't double-spawn.
- Output is capped (bytes + wall-clock + PID limits); truncation is signaled explicitly.
- `POST /v1/sandboxes/{id}/runs` is the same contract against a longer-lived sandbox
  (multiple runs against one warm sandbox) instead of the one-shot ephemeral convenience.

### 5.2 Interactive PTY (standalone-user path) — live, single-viewer

- `WS /v1/sandboxes/{id}/attach` → bidirectional. Client frames: `stdin`, `resize
  {cols,rows}`, `signal`. Server frames: `stdout`, `stderr`, `exit`. Backed by an `exec`
  with `tty=true` onto a restricted shell in `/workspace`.
- **Exactly one active viewer per sandbox.** A second concurrent attach attempt is
  rejected (`409`) rather than multiplexed — no collaboration in v1.
- Heartbeats + idle detection feed the TTL reconciler. Disconnect ≠ destroy (grace
  window), so a dropped socket can reattach as the *same* viewer.

### 5.3 Variable/state capture ("global variable dump")

Batch runs can report the program's final variable state alongside stdout/stderr, when
the language component declares `provides.batchRunner.supportsVariableDump: true`. The
contract: each such component ships a **batch runner** wrapper (not the raw interpreter)
as its `defaultRun` for batch mode:

1. Feed the up-front stdin, then close it (see §5.1).
2. Execute user code inside a dedicated namespace/scope (not the runner's own globals).
3. Capture stdout/stderr via redirected streams (not the parent process's).
4. On completion (success or exception), serialize the final top-level scope to JSON —
   skipping modules/functions/unserializable objects (falling back to `repr()` for
   anything not JSON-native) — and write it to a well-known path
   (`/tmp/.kubesandbox_vars.json`) inside the sandbox.
5. The provisioner reads that file after the process exits and folds it into
   `BatchRunResult.variables`; absent/unparsable → `null`.

Python implements this now (`components/languages/python/runner.py`, §8). The same
contract generalizes to other languages later; it's opt-in per component, not assumed.

### 5.4 Files

- `PUT /v1/sandboxes/{id}/files` (upload/tar), `GET .../files?path=` (download), `GET .../tree`.
  Bounded by workspace size + path allowlist.

---

## 6. Security & isolation (defense in depth)

**No root, no admin — ever.** Enforced structurally, not by blocklists.

**Layer 1 — Pod/container hardening (all environments):**
- `runAsNonRoot: true`, `runAsUser/Group: 10001`, `allowPrivilegeEscalation: false`
- `capabilities: { drop: ["ALL"] }`, `seccompProfile: RuntimeDefault`
- `readOnlyRootFilesystem: true` + writable `emptyDir` only for `/workspace`, `/tmp`
- `automountServiceAccountToken: false`, no host mounts, no hostNetwork/PID/IPC
- resource `requests`/`limits` (cpu, memory, **ephemeral-storage**), PID limits, ulimits

**Layer 2 — Kernel isolation (prod, configurable):**
- `runtimeClassName: gvisor` (or Kata) in `aks-prod`; standard runtime in `local`.
- Requires a node pool that supports the chosen runtime (documented in deploy).

**Layer 3 — Namespace & policy:**
- Namespace-per-sandbox (or per-tenant) with `ResourceQuota` + `LimitRange`.
- **`NetworkPolicy` default-deny** ingress/egress; DB sidecars reachable only intra-pod.
  The *exact* allowlist (if any) is authored and owned entirely by the deployment
  overlay per environment (§7, §12) — components only express an egress **intent**
  (e.g. "wants package-mirror access"), never the enforcement itself.
- `PodDisruptionBudget`/priority as needed.

**Layer 4 — Supply/package control:**
- Package installs (pip/npm/…) routed through an internal mirror/proxy (Artifactory/Nexus),
  with optional allow/deny lists per component.

**Layer 5 — Application guardrails:**
- Restricted shell (`/workspace` chroot-like workdir), forbidden-command hints (best-effort;
  real enforcement is capability/network/user restrictions, not string matching).
- Per-run wall-clock, output-size, and process caps.
- Full **audit log** of every command/run (who, what, when, exit code) in Postgres.

**Layer 6 — Control-plane security:** authN/Z (§11), rate limits + quotas, input validation
(Pydantic), secrets via Azure Key Vault / K8s Secrets (never in manifests), least-privilege
RBAC for the control plane's own service account (only what it needs to manage sandbox namespaces).

> **Honest note:** command denylists and `readOnlyRootFilesystem` are hardening, not
> primary boundaries. The real containment comes from non-root + dropped caps + network
> policy + (in prod) gVisor/Kata. The plan treats blocklists as UX/defense-in-depth only.

---

## 7. Configuration & environments

**Only two environments exist: `local` and `aks-prod`.** Per management directive, dev
and prod are exact clones and no separate dev *service* is stood up — there is no
`aks-dev` profile. Any pre-prod validation happens as a namespace/tenant boundary inside
`aks-prod` itself (or in `local`), not as a third config profile.

**Layered config** via `pydantic-settings`:

```
defaults (in code)  ◀  config/settings/<env>.yaml  ◀  env vars  ◀  secrets (KeyVault/K8s)
```

A single `APP_ENV ∈ {local, aks-prod}` selects the profile. Per-env differences are data,
not code branches:

| Concern | `local` | `aks-prod` |
|---|---|---|
| Provisioner | Docker (+ optional `kind` for K8s-spec parity testing) | Kubernetes (AKS) |
| Runtime isolation | standard | gVisor/Kata |
| Image registry | local `registry:2` container (ACR-shaped, pull-based) | Azure Container Registry |
| Persistence | off by default, PVC-equivalent bind mount optional | per-tier, PVC |
| Package egress | resolved by the `local` deploy overlay | resolved by the `aks-prod` deploy overlay |
| Secrets | `.env` / local file | Azure Key Vault (CSI) |
| Scaling | 1 replica | HPA + PDB |
| Billing mode | credit or PAYG, per tenant (same as prod — just smaller numbers) | credit or PAYG, per tenant |

Components/templates also carry per-env `overrides` and `compatibility` gates, so the same
manifest behaves correctly across both environments.

---

## 8. Packaging / build system — golden images

The `source.type` on a component selects a **build strategy**; each produces an artifact.
Regardless of which strategy a component author picked, the *output* is always the same
thing: an immutable, fully-baked **golden image**, built entirely at **publish time**
(CI), never inside a request path. This is what makes pre-warmed pods unnecessary purely
for cold-start-avoidance reasons (§4.3 pooling exists for scheduling/throughput, not to
hide build latency — there isn't any at request time).

| `source.type` | How it's built | Tooling |
|---|---|---|
| `image` | Use a prebuilt image as-is | registry pull |
| `dockerfile` | Rootless build → push | **Kaniko** / BuildKit |
| `compose` | Translate `docker-compose.yaml` → multi-container pod / sidecars | **kompose**-style translator |
| `pipeline` | Ordered build steps (fetch → build → test → package) | internal `BuildPipeline` runner |
| `helm` | Render a chart for service-type components | `helm template` |

`BuildManager` normalizes all strategies behind one interface:

```python
class BuildStrategy(Protocol):
    async def build(self, component: Component, ctx: BuildContext) -> Artifact: ...
```

Runtime configuration (mirror URLs, feature flags, resource-class labels, egress-intent
hints) is injected via **ConfigMap/env at pod-creation time**, never baked into the image
— so the same golden image is portable across environments and future clouds.

### 8.1 Image registry & local parity

| | `local` | `aks-prod` |
|---|---|---|
| Registry | `LocalImageStore`: a `registry:2` container, pull-based like ACR | `ACRRegistryProvider`: Azure Container Registry |
| K8s-spec testing | optional `kind` cluster, `kind load docker-image` from the local store | native |
| Fallback | plain `docker build` straight into the local daemon when the local registry isn't running | n/a |

A `pipeline` manifest example (for a component assembled from steps):

```yaml
source:
  type: pipeline
  pipeline:
    steps:
      - name: fetch    run: "git clone --depth 1 https://... src"
      - name: compile  run: "make -C src build"
      - name: package  run: "kaniko --dockerfile src/Dockerfile --destination $IMAGE"
    cache: { key: "{name}-{version}", store: object }
```

Control-plane deploy uses **Helm + Kustomize overlays**; sandbox primitives (NetworkPolicy,
RBAC, quotas, RuntimeClass) live as a Kustomize base with `local`/`aks-prod` overlays.

---

## 9. Cloud abstraction (Azure now, others pluggable)

A `CloudProvider` bundle isolates every cloud-specific integration behind an interface,
so the *only* Azure-only decision is "which implementation is registered" — not
scattered `if cloud == "azure"` branches.

```python
class SecretsProvider(Protocol):
    async def get(self, name: str) -> str: ...
class ObjectStorageProvider(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
class ImageRegistryProvider(Protocol):
    async def push(self, ref: str, artifact: Artifact) -> str: ...
    async def resolve(self, ref: str) -> str: ...
```

| Concern | Azure (implemented) | AWS / GCP |
|---|---|---|
| Secrets | `AzureKeyVaultSecretsProvider` | stub — `NotImplementedError("AWS Secrets Manager support coming soon")` (same shape for GCP Secret Manager) |
| Object storage | `AzureBlobStorageProvider` (prod), `MinIOStorageProvider` (local, S3-compatible so it doubles as the local stand-in for S3 later) | stub — `"S3/GCS support coming soon"` |
| Image registry | `ACRRegistryProvider` (prod), `LocalImageStore` (local) | stub — `"ECR/Artifact Registry support coming soon"` |
| Identity | Azure AD OIDC | stub |

Stubs fail loudly and immediately (raise, not silent no-op) so a misconfiguration is
caught at startup/config-validation time, not mid-request. `local` never needs a cloud
provider at all — it uses the local/self-hosted equivalents (`MinIO`, `LocalImageStore`,
`.env` secrets) end to end, so a laptop with Docker is sufficient for full-stack testing.

---

## 10. Backend service design (FastAPI)

**Layered, dependency-injected:**

- **API layer** — versioned routers (`/v1/...`), request/response Pydantic schemas, auth deps.
- **Service layer** — `SandboxService`, `ComponentRegistryService`, `TemplateService`,
  `EntitlementService`, `PoolManager`, `BuildManager`, `AuthService`, `QuotaService`,
  `BillingService`.
- **Provisioner layer** — `Provisioner` adapters (§4.2).
- **CloudProvider layer** — Secrets/ObjectStorage/ImageRegistry adapters (§9).
- **Streaming layer** — WS gateway (PTY), exec/batch stream plumbing.
- **Persistence layer** — SQLAlchemy 2.0 async repositories + Redis client.
- **Extensions layer** — manifest loader/validator, hook resolver/sandboxed loader.
- **Reconciler** — TTL/GC/desired-state/pool-replenishment loop (separate worker process).

Concurrency: fully async I/O; CPU-bound bits (manifest rendering, tar) offloaded to a
thread pool; builds and long jobs to a task queue (`arq`) so the API stays responsive.

### 10.1 Data model & persistence

**PostgreSQL (metadata + audit + billing)** — core tables:

`tenants` · `users` · `api_keys` (hashed) · `components` + `component_versions` ·
`templates` · `component_entitlements` · `publish_grants` · `sandboxes` (session records,
state, TTLs, handle refs) · `runs` (batch executions, exit codes, timings, variable dumps)
· `workspaces` (persistent workspace metadata, quota, last-access) · `builds` · `quotas` ·
`audit_logs` · `pool_state` · `billing_accounts` · `pricing_rules` · `usage_records` ·
`credit_wallets` · `credit_ledger` · `invoices`.

**Redis** — session/attach registry, WS heartbeats, idle timers, rate limiting, pool
claim locks, short-lived reconciler locks.

**Object store (Blob/MinIO)** — run logs (overflow), file artifacts, build cache,
archived (post-idle) persistent workspaces.

Migrations via **Alembic**. The component registry's source of truth is git (`components/`);
Postgres holds an indexed/queryable projection refreshed on load/reload.

### 10.2 Persistent workspaces — quotas & retention (generous defaults, admin-tunable)

| Setting | Default | Notes |
|---|---|---|
| Quota per user | **10 GiB** | Overridable per tenant/tier by an admin |
| Idle retention (fully available) | **30 days** since last session activity | After this, the workspace is archived, not deleted |
| Archive grace period | **60 more days** (90 total idle) | Archived = moved to cold object storage, detachable on demand, not attached to a running pod |
| Hard delete | **90 days** total inactivity | Deleted after the archive grace period lapses with no access |
| Absolute max lifetime | **365 days**, regardless of activity | Requires explicit renewal (user or admin) past this point, else follows the same archive→delete path |

These are deliberately generous starting margins, not hard product commitments — they
live in `config/settings/{local,aks-prod}.yaml` and a per-tenant override table, so an
admin can tighten/loosen them without a code change.

---

## 11. AuthN / AuthZ & multi-tenancy

- **Standalone users:** OIDC (Azure AD) → short-lived JWT session.
- **Workflow-builder:** service accounts with **API keys** (hashed at rest) or client-credentials
  OAuth; scoped to a tenant.
- **RBAC:** roles `admin` (full registry access, entitlement/publish-grant management,
  billing mode/pricing config), `operator`, `user` (create/run/attach own sandboxes,
  limited to their entitled catalog). Enforced as FastAPI dependencies.
- **Tenancy:** every sandbox/run/quota/billing record is tenant-scoped; namespace labels
  carry tenant id; quotas (max concurrent sandboxes, cpu/mem, monthly minutes, credit
  balance/spend cap) enforced by `QuotaService`/`BillingService` before create. Rate
  limiting per key/user.

---

## 12. Networking model

- Sandboxes have **no inbound** exposure. All interaction is mediated by the control plane
  (exec/attach).
- Default-deny is the floor everywhere; the specific egress allowlist (if any — e.g. a
  package mirror) is **entirely a deployment-overlay concern**, authored per environment
  in `deploy/manifests/overlays/{local,aks-prod}`, not computed or decided by the control
  plane at sandbox-create time. Components only ever *declare intent*
  (`access.network.egress: intent-only`, with a free-text reason) which the deploy
  tooling can read as documentation/input — the running app never grants egress itself.
- DB/service sidecars are reachable **only within the same pod**.
- Control plane ↔ K8s API over in-cluster RBAC; ↔ Postgres/Redis over private networking.

---

## 13. Billing & costing

Two billing modes exist side by side; an admin selects one **per tenant**:

1. **Credit-based** — a tenant holds a credit wallet; usage (compute-seconds, memory,
   storage-days, DB add-on hours) is priced via `pricing_rules` and deducted from the
   wallet. Sandbox creation is **pre-authorized** against the wallet balance (or a
   configured spend cap) — insufficient credit blocks creation with a clear error before
   any resource is provisioned.
2. **Pay-as-you-go** — no pre-authorization (beyond an optional soft spend cap); usage
   accumulates in `usage_records` and rolls up into a draft `invoice` on a billing cycle.
   **Actual payment collection is a stub** — the system computes and records what is
   owed; wiring a payment gateway (Stripe, Azure billing, etc.) is a later integration,
   deliberately out of scope here.

```python
class CostingStrategy(Protocol):
    async def authorize(self, tenant_id: str, estimate: UsageEstimate) -> AuthResult: ...
    async def record_usage(self, tenant_id: str, event: UsageEvent) -> None: ...
    async def settle(self, tenant_id: str, period: BillingPeriod) -> None: ...
```

`CreditBillingStrategy` enforces `authorize()` hard; `PayAsYouGoBillingStrategy` treats it
as advisory (spend-cap only) and does the real work in `settle()` (invoice draft
generation). Both strategies write to the same `usage_records` table so reporting is
uniform regardless of mode. Admin API: `PATCH /v1/admin/tenants/{id}/billing {mode,
spend_cap?}`, `POST /v1/admin/pricing-rules`.

---

## 14. Observability & cost controls

- **Logging:** structured (`structlog`), correlation ids per sandbox/run, audit trail.
- **Metrics:** Prometheus (`sandboxes_active`, `provision_latency`, `run_duration`,
  `build_duration`, `pool_hit_rate`, quota/credit usage), plus OpenTelemetry traces
  spanning API → provisioner → pod.
- **Cost controls:** aggressive idle TTLs, right-sized requests/limits, node autoscaling,
  reap orphaned resources, per-tenant minute/credit quotas, optional spot node pools for
  sandboxes, workspace archive/purge per §10.2.

---

## 15. Deployment (configurable manifests, local + aks-prod)

```
deploy/
  helm/kubesandbox/            # control-plane chart (API, worker, reconciler)
  manifests/base/               # sandbox primitives: netpol, rbac, quota, limitrange, runtimeclass
  overlays/{local,aks-prod}/    # Kustomize overlays (isolation, replicas, secrets refs, egress rules)
  dockerfiles/                  # base sandbox image, control-plane image
```

- Control plane deployed via **Helm** (values per env); sandbox primitives via **Kustomize**
  base+overlays. GitOps-friendly (Argo/Flux) later.
- `local`: `docker-compose.yml` brings up Postgres, Redis, MinIO, the local image
  registry, and the app itself — no cluster required; an optional `kind` cluster mirrors
  the `aks-prod` manifests for K8s-spec-accurate testing before anything touches Azure.
- `aks-prod`: HPA on the API, PDBs, gVisor/Kata node pool (+ a separate segregated node
  pool for `heavy` weight-class sandboxes, §4.3), managed Postgres, Key Vault CSI, ACR.

---

## 16. Language & database rulesets (concrete)

**Language rules** (per component `access`): non-root execution; writable paths limited to
`/workspace` + `/tmp`; package installs via mirror with allow/deny lists; wall-clock + output
+ PID caps; egress intent declared, enforcement owned by the deploy overlay (§12). Example
— Python: `pip` via internal mirror, 60s wall-clock, 5 MB output, 128 procs. Node:
`npm`/`pnpm` via mirror, no global installs to read-only paths, same caps.

**Database rules** (enforced via roles/grants + policy, not just image config):
- Create a **non-superuser** role scoped to a per-sandbox database (Postgres: `sandbox_user`
  with `CONNECT, CREATE, TEMPORARY`; **denied** `SUPERUSER, CREATEROLE, REPLICATION,
  COPY … TO/FROM PROGRAM`, and no filesystem/`pg_read_file` access).
- `statement_timeout`, `max_connections`, and a max DB size guard.
- DB port reachable only intra-pod; credentials injected as `DATABASE_URL` env, never printed.
- MySQL/MariaDB analog: scoped user, no `FILE`/`SUPER`/`PROCESS` privileges, per-session db.

These live in the component manifests and are applied by the DB component's `on_provision` hook.

---

## 17. API surface (v1, illustrative)

```
POST   /v1/sandboxes                 create (from template or ad-hoc component list)
GET    /v1/sandboxes/{id}            status
DELETE /v1/sandboxes/{id}            destroy
POST   /v1/sandboxes/{id}/runs       start a batch run against a warm sandbox
GET    /v1/runs/{run_id}             bundled run status/result (poll target)
WS     /v1/sandboxes/{id}/attach     interactive PTY (single viewer)
GET/PUT /v1/sandboxes/{id}/files     file tree / upload / download
POST   /v1/execute                   one-shot ephemeral batch run (workflow-block convenience)

GET    /v1/components                list/query registry (entitlement-filtered)
GET    /v1/components/{name}         versions & schema
POST   /v1/components (admin)        register manifest
POST   /v1/components/{name}/build   trigger build
GET/POST /v1/templates               list / create blueprints

GET/PATCH /v1/admin/entitlements     manage per-tenant/user catalog visibility
GET/PATCH /v1/admin/publish-grants   manage who may publish private components
PATCH  /v1/admin/tenants/{id}/billing   set billing mode / spend cap
POST   /v1/admin/pricing-rules       configure unit pricing

GET    /healthz  /readyz  /metrics
```

**Added in Phase 9/9b** beyond the illustrative list above — the surface a browser UI
needs (see `docs/UI_INTEGRATION.md`), plus two endpoints listed here that had never
actually been implemented:

```
GET    /v1/auth/config               public OIDC config for a frontend (unauthenticated)
POST   /v1/auth/token                exchange an IdP token for a session JWT (§11)
GET    /v1/me                        caller identity, role, and enabled features

POST   /v1/execute?async=true        non-blocking variant (§5.1) — returns a run_id
GET    /v1/runs/{run_id}             bundled run status/result — the §5.1 poll target
GET    /v1/runs                      run history (paginated)
GET    /v1/sandboxes                 list own/tenant sandboxes (paginated)
GET    /v1/templates/{name}          template versions + resource/TTL shape
GET    /v1/builds                    build history (paginated)

POST/GET/DELETE /v1/api-keys         service-account key management (§11)
GET    /v1/workspaces/me             persistent workspace quota/usage/state (§10.2)
GET    /v1/billing/account           own billing mode, balance, month-to-date cost
GET    /v1/billing/usage             own priced usage records

GET    /v1/admin/pricing-rules       read back configured pricing
GET    /v1/admin/tenants             list tenants (counts, billing position)
GET    /v1/admin/users               list users
PATCH  /v1/admin/users/{id}/role     the only way to grant `admin` (§11)
```

Collections that grow with use return `{items, total, limit, offset}`; registry listings
stay bare arrays. Tenant isolation is reported as **404, never 403**, so ids can't be
probed.

A thin **client SDK** (`sdk/`, Python first) wraps these for the workflow-builder's code
block — a separately installable `kubesandbox-sdk` package whose only required
dependency is httpx, with sync and async clients, typed models, a status-code-to-exception
mapping, and the PTY protocol behind an optional `[attach]` extra.

---

## 18. Proposed repository layout

```
KubeSandbox/
  app/
    main.py
    api/v1/           # routers
    core/             # config, security, logging, errors
    domain/           # models, schemas, state machine
    services/         # sandbox, registry, template, entitlement, pool, build, auth, quota, billing
    provisioners/     # base.py, kubernetes.py, docker.py
    cloud/            # secrets.py, storage.py, registry.py — Azure impl + AWS/GCP stubs
    streaming/        # ws_gateway.py, pty.py
    persistence/      # db.py, repositories/, redis.py
    extensions/       # loader.py, hooks.py, validation.py
    reconciler/       # loop.py
  components/         # the registry (git = source of truth)
    languages/{python,node,go,...}/{component.yaml,hooks.py,Dockerfile,runner.*}
    databases/{postgresql,mysql,redis,...}/
    tools/{git,jq,...}/
  templates/         # SandboxTemplate blueprints
  build/             # build strategies, kaniko jobs, compose translator, pipelines
  deploy/            # helm/, manifests/base, overlays/{local,aks-prod}, dockerfiles/
  config/settings/   # local.yaml, aks-prod.yaml
  schemas/           # JSON Schema for Component/Template manifests
  sdk/               # client SDK for the workflow-builder
  tests/             # unit + integration (testcontainers) + e2e (kind)
  docs/
```

---

## 19. Technology choices

- **API:** FastAPI, Pydantic v2, `pydantic-settings`, native WebSockets.
- **Data:** SQLAlchemy 2.0 (async) + Alembic + PostgreSQL; `redis-py` (async).
- **Orchestration:** `kubernetes_asyncio` (or `lightkube`); `aiodocker` for local.
- **Auth:** Authlib/OIDC + JWT; hashed API keys.
- **Build:** Kaniko/BuildKit (rootless), kompose-style translator, Helm.
- **Isolation:** gVisor or Kata (prod runtimeClass).
- **Tasks:** `arq` for builds/long jobs.
- **Observability:** OpenTelemetry, Prometheus, `structlog`, Loki.
- **Testing:** pytest, testcontainers, kind for e2e.
- **Deploy:** Helm + Kustomize overlays; GitOps-ready.

---

## 20. Implementation roadmap (phased)

| Phase | Deliverable | Exit criteria |
|---|---|---|
| **0 — Foundations** | Repo scaffold, layered config (`local`/`aks-prod`), DB + migrations, auth skeleton, non-root base image, manifest JSON Schema | App boots per-env; schema validates |
| **1 — Local MVP** | `DockerProvisioner`, bundled batch runs, Python component + batch runner (with variable dump), `/execute` | Run Python end-to-end locally, bundled result incl. variable dump, exit code |
| **2 — Registry, templates & entitlements** | Manifest loader/validator, template composition, entitlement/publish-grant enforcement, more languages | Compose multi-language sandbox from manifests; a non-admin only sees their entitled catalog |
| **3 — Kubernetes + hardening** | `KubernetesProvisioner` (kind → aks-prod), pod security, NetworkPolicy via overlay, quotas | Untrusted run contained on a cluster |
| **4 — Interactive PTY** | WS attach, resize/signals, file APIs, reattach, single-viewer enforcement | Live terminal session for a user |
| **5 — Database add-ons** | Postgres/MySQL sidecars + scoped-role rulesets via hooks | psql to a scoped non-superuser db |
| **6 — Build system & golden images** | Dockerfile (Kaniko), compose translator, pipeline runner, helm render, ACR + LocalImageStore | Publish a component from each source type as a golden image |
| **7 — Pooling & persistence** | Pool Manager (weight classes, claim/recycle), PVC workspaces w/ quota+retention, reconciler | Warm-pool hit rate measurable; idle TTL + archive/purge working |
| **8 — Billing** | `BillingService`, credit + PAYG strategies, admin pricing/mode APIs | Sandbox creation blocked on insufficient credit; PAYG invoice draft generated |
| **9 — Prod hardening** | gVisor/Kata prod pool + heavy-workload node pool, HPA/PDB, cloud-provider stubs verified fail-fast, SDK, docs | Prod-ready, workflow-builder integrated |
| **9b — UI integration readiness** | CORS, OIDC/JWT session auth (§11), the read/list surface a frontend needs, `?async=true` + `GET /v1/runs/{id}` (§5.1/§17, previously unimplemented), API-key management, pagination | A browser UI can authenticate, enumerate, and drive the platform without backend changes |

**Phase 9b was added after the original plan**, once UI development went on the roadmap:
building the UI is a separate stage, but the backend contract it needs is not, and an
audit turned up six blockers — two of them endpoints §17/§5.1 already specify. See
`docs/TASK_CHECKLIST.md` for the item-by-item status and `docs/UI_INTEGRATION.md` for
the contract itself.

---

## 21. Remaining open items

Everything previously open has been resolved by explicit decision (§1 tenets, §4.3
pooling, §7 two environments, §8 golden images, §9 cloud stubs, §10.2 workspace
quotas/retention, §12 egress-by-overlay, §13 billing). Nothing structurally undecided
remains for v1; remaining unknowns are implementation-level (exact pricing numbers, node
pool sizing, specific mirror URLs) and will surface naturally per-environment during
Phase 6–9 rather than blocking the design.

**Closed after Phase 9**, in a follow-up hardening pass — all five cross-cutting items
plus the admin bootstrap gap. `AuditService` makes §6 Layer 5 true, `QuotaService` adds
§11's ceilings (and the `quotas` table §10.1 listed but nothing had ever created),
`RateLimiter` adds §11's throttling, and `auth.bootstrap_admin_emails` + `app/cli.py
seed-admin` remove the need for a direct DB write to mint the first admin. That pass also
audited the sandbox against a standard 13-point hardening checklist — see
`docs/SECURITY_HARDENING.md` for the item-by-item result.

**Still open**, all recorded with their reasoning in `docs/TASK_CHECKLIST.md` and
`docs/SECURITY_HARDENING.md`:

- **Kubernetes per-pod PID limits** — §6's fork-bomb protection is enforced on the Docker
  backend but has no pod-level equivalent in Kubernetes; it is a kubelet setting
  (`podPidsLimit`) owned by whoever provisions the node pool. Not closeable from
  application code; documented as a required setting in `deploy/azure/README.md`.
- **No egress proxy / package mirror** — §12 correctly makes the allowlist an overlay
  concern, but nothing ships one, so a deployment needing `pip install` must provide it.
- **No log shipping configured** — the audit table is queryable but not tamper-resistant
  on its own (anyone who can reach the database can edit it), so §6's intent depends on a
  shipped copy that this repo doesn't set up.
- **Full run logs in object storage** (§10.1) — `runs` keeps a 10 KB excerpt per stream
  and nothing writes or serves the overflow.
- **Async-run durability** — a control-plane restart leaves an in-flight `?async=true`
  run marked `running` forever; nothing resumes or reaps it.
- **Quota accounting is approximate** — per-weight-class budgets rather than each live
  sandbox's resolved limits, because `sandboxes` doesn't persist resolved cpu/memory.
  Conservative (errs toward refusing), and the exact fix is a schema change.
```
