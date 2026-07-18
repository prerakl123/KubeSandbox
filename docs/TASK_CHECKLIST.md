# KubeSandbox — Exhaustive Task Checklist

Every task implied by `docs/ARCHITECTURE_AND_PLAN.md`, broken out phase by phase (§20),
with an honest per-item completion status as of 2026-07-18. This is a granular expansion
of the roadmap table, not a re-statement of it — each phase's one-line "deliverable"
below is exploded into the actual concrete pieces of work it implies across the rest of
the doc (§3–§19).

**Summary: 40 / 103 items complete — all of it inside Phase 0 (19/21) and Phase 1
(21/21, fully live-verified). Phases 2–9 and the cross-cutting section are 0% started.**
Phase 1 is no longer just "built" — it has been driven end-to-end against real Docker,
real Postgres, and the actual golden image, via `POST /v1/execute` returning correct
`stdout`/`stderr`/`exit_code`/`variables` with no leaked containers afterward. See
"Bugs found and fixed during live verification" at the end of this section — none of
these were catchable by the unit tests alone, precisely because they needed a real
Docker daemon to surface.

---

## Phase 0 — Foundations (19/21)

- [x] Repo scaffold matching the intended layout (doc §18)
- [x] `pyproject.toml` + `uv`-managed dependencies (fastapi, uvicorn, pydantic(-settings),
      sqlalchemy[asyncio], asyncpg, alembic, redis, aiodocker pinned to `0.27.0`,
      structlog, pyjwt, pyyaml, jsonschema, python-multipart; dev: pytest,
      pytest-asyncio, httpx, aiosqlite)
- [x] `app/core/config.py` — layered `Settings` (code defaults ← YAML ← env vars),
      `APP_ENV` gated to exactly `{local, aks-prod}`
- [x] `config/settings/local.yaml`
- [x] `config/settings/aks-prod.yaml`
- [x] Guard rejecting `auth.disabled=true` outside `app_env=local`
- [x] `app/core/logging.py` — structlog console/JSON renderer
- [x] `app/core/errors.py` — `KubeSandboxError` domain exception hierarchy
- [x] `app/main.py` — FastAPI app factory + lifespan (registry load, provisioner
      construction/teardown)
- [x] `/healthz`, `/readyz` endpoints
- [x] Exception handlers mapping domain errors → HTTP status codes (404/403/429/502/400)
- [x] `schemas/component.schema.json`
- [x] `schemas/template.schema.json`
- [x] `app/domain/manifests.py` — typed `Component`/`SandboxTemplate` pydantic models
- [x] `app/extensions/loader.py` — manifest loader + JSON Schema validation + semantic
      cross-reference validation (`requires`, template `base`/`components` refs)
- [x] `app/persistence/models.py` — full ORM schema, all 18 tables from doc §10.1
- [x] `app/persistence/db.py` — async engine/session factory
- [x] Alembic scaffold (`alembic/env.py` wired to `Settings.database.dsn` + our
      `Base.metadata`, async-native, no second sync DB driver needed)
- [x] Initial migration generated + verified to upgrade **and** downgrade cleanly
      (against a throwaway SQLite DB — no live Postgres was available to generate
      against; a SQLite-specific `DEFAULT` rendering was caught and fixed before it
      could reach Postgres)
- [ ] A generic non-root **base image/component** (`category: base`) that a
      `SandboxTemplate.spec.base.ref` could point to — never created; no
      `SandboxTemplate` YAML file exists anywhere in the repo either
- [ ] OIDC/JWT session auth for standalone human users (doc §11) — `pyjwt` is an
      installed dependency but there is no JWT-issuing or JWT-validating code anywhere;
      only the hashed-API-key path (service accounts) is implemented

---

## Phase 1 — Local MVP (21/21 — fully live-verified)

- [x] `app/provisioners/base.py` — the `Provisioner` Protocol
- [x] `app/provisioners/resources.py` — cpu/memory quantity parsing, unit-tested
- [x] `app/provisioners/docker.py` — `DockerProvisioner`: `acquire`/`exec_batch`/
      `status`/`put_files`/`recycle`/`destroy`
- [x] Graceful teardown: SIGTERM-with-grace before force-remove, idempotent, and
      `acquire()` cleans up a container that was created but failed to start rather
      than leaking it
- [x] stdin-entirely-up-front-then-EOF handling (`_half_close_stdin`, reaching into
      aiodocker internals since there's no public API for it) — verified in isolation
      with a fake stream object, not against a live container
- [x] Output-byte-cap enforcement that keeps draining (so a truncated run still
      reports its real exit code) instead of aborting the read loop
- [x] `components/languages/python/component.yaml`
- [x] `components/languages/python/Dockerfile` (non-root uid 10001)
- [x] `components/languages/python/runner.py` — batch runner with variable-dump
      capture; verified directly via subprocess (success, exception-with-partial-state,
      and no-stdin-gives-immediate-EOF cases all behave per spec)
- [x] `app/services/sandbox_service.py` — resolve component → build spec/command →
      acquire → exec_batch → destroy (`try`/`finally`) → persist `Sandbox` + `Run` rows
- [x] `app/api/v1/execute.py` — `POST /v1/execute`
- [x] `docker-compose.yml` — Postgres, Redis, MinIO, local registry for local dev infra
- [x] `deploy/dockerfiles/Dockerfile.control-plane` (for future K8s-parity testing;
      not used by `docker-compose.yml` on purpose)
- [x] `README.md` with full local setup steps
- [x] Unit tests: config (4), loader (6), sandbox_service with a `FakeProvisioner` (4)
      — 14 tests, all passing
- [x] Docker integration test file written (stdin/EOF, variable dump, exception
      capture) — skips itself cleanly with an actionable reason when Docker/the image
      aren't available
- [x] **Live end-to-end verification** — `docker` group access fixed (`newgrp docker`);
      `POST /v1/execute` driven repeatedly against the real stack with correct
      `stdout`/`stderr`/`exit_code`/`variables` and stdin round-tripping correctly
- [x] The golden image was actually built (`docker build -t
      kubesandbox/python:3.12.4-slim components/languages/python`) and rebuilt again
      after a `runner.py` fix
- [x] `docker compose up -d` actually run — Postgres/Redis/MinIO/registry all healthy
- [x] The Alembic migration applied to the real Postgres instance — all 19 tables
      confirmed via `psql -c '\dt'`
- [x] The `curl /v1/execute` example actually executed, repeatedly, ending in a fully
      correct bundled result
- [x] Graceful eradication confirmed live: `docker ps -a --filter name=kubesandbox-`
      shows zero leaked sandbox containers after multiple runs — only the 4 infra
      containers remain

### Bugs found and fixed during live verification

None of these were visible to the unit tests (which use a `FakeProvisioner`) — they only
surfaced once `DockerProvisioner` ran against a real daemon:

1. **`put_files` used `put_archive` (the `docker cp`-into-container API), which Docker
   refuses outright on any container created with `ReadonlyRootfs: true`** — even though
   the destination was a separate writable tmpfs mount. Fixed by writing files via
   `exec` (piping content to `cat` over stdin) instead — a process running *inside* the
   container can write to that tmpfs mount with no such restriction.
2. **Every `container.exec()` call was missing an explicit `user=`.** aiodocker defaults
   exec to root when it's omitted (per its own docstring) — meaning the actual user code
   execution, not just the file-write bug above, was silently running as root, directly
   violating the "no root, ever" tenet (doc §1, §6). Fixed by adding a
   `_SANDBOX_EXEC_USER = "10001:10001"` constant and passing it to all three exec sites
   (batch execution, file writes, workspace recycling).
3. **Tmpfs mounts had no explicit `uid=`/`gid=`/`mode=`**, so they defaulted to
   root-owned — uid 10001 (the sandbox's user, per fix #2 above) then couldn't write
   into its own `/workspace`. Fixed by mounting with
   `uid=10001,gid=10001,mode=0755` explicitly.
4. **`_read_variable_dump` used `get_archive` (`docker cp`-from-container), which
   couldn't find a file that demonstrably existed** (the runner wrote it with no
   exception) — the same class of archive-API-vs-tmpfs limitation as #1, just on the
   read side. Fixed the same way: read via `exec` (`cat`) instead of the archive API.

**Takeaway for later phases:** Docker's archive/copy endpoints (`put_archive`/
`get_archive`) are unreliable against tmpfs-mounted paths on `ReadonlyRootfs`
containers — this codebase never uses them again for sandbox I/O, only `exec`. Worth
keeping in mind if Phase 3's `KubernetesProvisioner` ever considers an analogous
copy-based API instead of `exec`.

---

## Phase 2 — Registry, templates & entitlements (0/10)

- [ ] `SandboxTemplate` composition/rendering into a multi-component spec —
      `SandboxService` currently only resolves a single ad-hoc language component; the
      template loader/schema exist (Phase 0) but nothing consumes a template end-to-end
- [ ] Additional language components beyond `python` — Node.js, Deno, Bun, Go, Rust,
      Java/JDK, .NET, Ruby, PHP, Bash/POSIX, Lua, R, Julia, TypeScript (doc §3.1)
- [ ] `GET /v1/components` (list/query registry, entitlement-filtered)
- [ ] `GET /v1/components/{name}` (versions & schema)
- [ ] `POST /v1/components` (admin: register a manifest)
- [ ] `GET/POST /v1/templates` (list / create blueprints)
- [ ] `EntitlementService` — filtering registry/template listings by tenant/user
      entitlements (the `component_entitlements`/`publish_grants` tables exist from
      Phase 0 but nothing reads or writes them)
- [ ] `GET/PATCH /v1/admin/entitlements`
- [ ] `GET/PATCH /v1/admin/publish-grants`
- [ ] Private, tenant-namespaced component publishing (`tenant/<id>/<name>`)

---

## Phase 3 — Kubernetes + hardening (0/6)

- [ ] `KubernetesProvisioner` — currently `app/main.py`'s `_build_provisioner` raises
      `NotImplementedError` for `provisioner.backend == "kubernetes"`
- [ ] Pod security context rendering as real Kubernetes PodSpec fields (non-root,
      dropped capabilities, seccomp, `readOnlyRootFilesystem` — all exist as *Docker*
      HostConfig fields in `DockerProvisioner` today, nothing K8s-specific yet)
- [ ] `deploy/manifests/base/` — NetworkPolicy, RBAC, ResourceQuota, LimitRange,
      RuntimeClass manifests (directory exists, empty)
- [ ] `deploy/overlays/local/` and `deploy/overlays/aks-prod/` Kustomize overlays
      (directories exist, empty)
- [ ] gVisor/Kata `RuntimeClass` wiring (the `provisioner.runtime_class` config field
      exists and defaults to `gvisor` in `aks-prod.yaml`, but nothing consumes it yet)
- [ ] `kind`-cluster-based Kubernetes-spec parity testing setup

---

## Phase 4 — Interactive PTY (0/7)

- [ ] `WS /v1/sandboxes/{id}/attach`
- [ ] `PTYStream` implementation in `DockerProvisioner` (currently `raise
      NotImplementedError` — the method exists purely as a documented placeholder)
- [ ] `PTYStream` implementation in `KubernetesProvisioner`
- [ ] `resize`/`signal` client-frame handling
- [ ] Single-viewer enforcement (409 on a concurrent second attach attempt)
- [ ] Reattach-after-disconnect grace window
- [ ] `GET/PUT /v1/sandboxes/{id}/files`, `GET /v1/sandboxes/{id}/tree`

---

## Phase 5 — Database add-ons (0/6)

- [ ] `components/databases/postgresql/component.yaml` — the directory exists but is
      empty; doc §3.3 shows an example manifest, no file was ever actually written
- [ ] `components/databases/mysql/` component
- [ ] A `redis`-as-database-add-on component (distinct from Redis-as-control-plane-infra
      in `docker-compose.yml`)
- [ ] Multi-container (main + sidecar) composition in any provisioner —
      `DockerProvisioner` currently runs exactly one container per sandbox
- [ ] A concrete `ComponentHook` Python module (the `Protocol` is defined in doc §3.5;
      no hook module exists anywhere in `components/`)
- [ ] DB-scoped-role provisioning logic (non-superuser `sandbox_user`, limited grants,
      `statement_timeout`, `maxConnections`, max DB size guard)

---

## Phase 6 — Build system & golden images (0/8)

- [ ] `BuildManager` service
- [ ] `BuildStrategy` implementation: `dockerfile` (Kaniko/BuildKit)
- [ ] `BuildStrategy` implementation: `compose` (kompose-style translator)
- [ ] `BuildStrategy` implementation: `pipeline` (internal step runner)
- [ ] `BuildStrategy` implementation: `helm` (chart render)
- [ ] `ACRRegistryProvider`
- [ ] `LocalImageStore` actually wired up — `docker-compose.yml` defines a `registry:2`
      service, but nothing pushes to or pulls from it; `DockerProvisioner` runs images
      straight out of the local Docker daemon's cache
- [ ] Any component published as a golden image via an automated pipeline — the
      `python` component's image is still a documented one-time manual `docker build`
      (see README.md), not something a `BuildManager` produced

---

## Phase 7 — Pooling & persistence (0/6)

- [ ] `PoolManager` service
- [ ] Weight-class-based node-pool/queue segregation (light/standard/heavy)
- [ ] Warm-pool claim wiring inside `acquire()` — `DockerProvisioner.recycle()` exists
      and is unit-tested in isolation, but nothing ever calls it; `SandboxService`
      always destroys ephemeral sandboxes instead of recycling them
- [ ] PVC-backed (or bind-mount-backed, locally) persistent workspaces — the
      `workspace.persistence_enabled` config flag exists and is unused
- [ ] Workspace quota/retention enforcement — archive job, purge job (the `Workspace`
      table exists from Phase 0; nothing reads or writes it)
- [ ] `app/reconciler/loop.py` — TTL/GC/desired-state background loop (directory
      exists, empty placeholder)

---

## Phase 8 — Billing (0/6)

- [ ] `BillingService`
- [ ] `CreditBillingStrategy`
- [ ] `PayAsYouGoBillingStrategy`
- [ ] `PATCH /v1/admin/tenants/{id}/billing`
- [ ] `POST /v1/admin/pricing-rules`
- [ ] Usage-event recording wired into `SandboxService.execute()` — every billing/usage
      table (`billing_accounts`, `pricing_rules`, `usage_records`, `credit_wallets`,
      `credit_ledger`, `invoices`) exists from Phase 0; none of them are ever written to

---

## Phase 9 — Prod hardening (0/7)

- [ ] gVisor/Kata prod node pool provisioning (cluster/infra-level, outside this repo's
      code, but referenced by `aks-prod.yaml`)
- [ ] Heavy-workload segregated node pool
- [ ] HPA/PDB manifests
- [ ] `app/cloud/` implementations — `SecretsProvider`, `ObjectStorageProvider`,
      `ImageRegistryProvider` (Azure real + AWS/GCP "Coming Soon" stubs per doc §9) —
      the directory exists with only an empty `__init__.py`; none of these interfaces
      or implementations have any code yet
- [ ] `sdk/` — client SDK for the workflow-builder (directory exists, empty)
- [ ] `deploy/helm/kubesandbox/` — control-plane Helm chart (directory exists, empty)
- [ ] Prometheus `/metrics` + OpenTelemetry tracing

---

## Cross-cutting — not owned by a single phase (0/5)

- [ ] `AuditLog` writes — the table exists from Phase 0; no service writes an entry for
      any action yet (`SandboxService` currently only writes `Sandbox`/`Run` rows)
- [ ] `QuotaService` — concurrent-sandbox caps, cpu/mem quotas, monthly-minute quotas
- [ ] Rate limiting per API key/user
- [ ] API-key issuance/management endpoints — the `ApiKey` table and hashed-lookup auth
      dependency exist and work, but nothing can create or revoke a key except a
      direct DB insert
- [ ] `GET /metrics`
