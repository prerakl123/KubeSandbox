# KubeSandbox — Exhaustive Task Checklist

Every task implied by `docs/ARCHITECTURE_AND_PLAN.md`, broken out phase by phase (§20),
with an honest per-item completion status as of 2026-08-28. This is a granular expansion
of the roadmap table, not a re-statement of it — each phase's one-line "deliverable"
below is exploded into the actual concrete pieces of work it implies across the rest of
the doc (§3–§19).

Where a phase's work could not be exercised against real infrastructure, that is stated
per item rather than glossed — see each phase's own "Live verification" section. Phase 9
in particular has an unusually wide gap (no reachable Postgres, no `helm`, no Azure this
session) and a short list of required follow-up commands.

**Summary: 103 / 103 roadmap items complete, plus a Phase 9b that wasn't in the
original plan:**
- Phase 0 (21/21) — its last open item, OIDC/JWT session auth, was closed by Phase 9b
- Phase 9 (7/7) + Phase 9b (UI integration readiness) + a follow-up hardening pass
- 557 unit tests passing, up from 284 at the end of Phase 8
- All 5 cross-cutting items now closed, plus an admin-bootstrap mechanism and a
13-point sandbox-hardening audit (`docs/SECURITY_HARDENING.md`)
- Phase 1 (21/21, fully live-verified)
Phase 1 is no longer just "built" — it has been driven end-to-end against real Docker,
real Postgres, and the actual golden image, via `POST /v1/execute` returning correct
`stdout`/`stderr`/`exit_code`/`variables` with no leaked containers afterward. See
"Bugs found and fixed during live verification" at the end of this section — none of
these were catchable by the unit tests alone, precisely because they needed a real
Docker daemon to surface.
- Phase 2 (10/10, fully live-verified)
Phase 2 was implemented and unit-tested (44 new unit tests, all passing) in a
session without live Docker daemon access, then live-verified end-to-end in a
follow-up pass once the user had Docker access again: all three new golden images
(`base`, `node`, `go`) were built, and `POST /v1/execute` was driven repeatedly against
the real stack for every language (`python`, `node`, `go`, `bash`) plus the composed
`base-dev-lab@1.0` template (base + bash + git sharing one image), each returning a
correct bundled result. `GET /v1/components` and `GET /v1/templates` were also hit live
and returned the full unfiltered catalog as the local-dev admin principal, as expected.
See "Known scope boundaries" below for what Phase 2 deliberately does *not* cover, and
the "5th bug" entry under Phase 1's live-verification bugs for a real `DockerProvisioner`
fix this live pass surfaced (broke `go run` specifically, but was a latent bug in every
prior phase — see below).
- Phase 3 (6/6, fully live-verified against a real kind cluster)
- Phase 4 (7/7, implemented, unit-tested — 100 unit tests passing — and live-verified against a real Docker
daemon via a relayed hand-off loop, since this session has no direct Docker/kind
access)
- Phase 5 (6/6, implemented, unit-tested — 137 unit tests passing — and fully 
live-verified against all three real database engines via the same relayed 
hand-off pattern as Phase 4: DSN injection, each scoped non-superuser role/ACL user,
and a rejected privilege-escalation attempt confirmed against live `postgres:16-alpine`,
`mysql:8.4`, and `redis:7-alpine` sidecars; four bugs found and fixed along the way, 
see Phase 5's "Bugs found and fixed during live verification")
- Phase 6 (8/8, implemented, unit-tested — 22 new unit tests, 159 total, passing —
and live-verified against the real Docker daemon/local registry/MinIO for three of
the four strategies)
`jq` (dockerfile), `ripgrep` (compose), and `httpie` (pipeline, including a real
MinIO-backed cache hit that skipped re-running its steps on a second build) were each
built, pushed, and confirmed via `POST /v1/components/{name}/build` +
`GET /v1/builds/{id}` against the real stack. Two real bugs found and fixed along the
way (a container-not-yet-running race in `DockerProvisioner`, and
`MinIOStorageProvider.get()` not handling a not-yet-existing bucket) — see Phase 6's
"Bugs found and fixed during live verification". A third issue surfaced — the
verification machine's snap-packaged Docker conflicts with `no-new-privileges` at the
AppArmor level, blocking sandbox *execution* (not building) of any component,
old or new — root-caused as a host Docker-packaging issue, not a KubeSandbox bug (see
Phase 6's "Known environment limitation found"). `demo-echo` (helm) and
`ACRRegistryProvider`/`AzureBlobStorageProvider` remain unverified live exactly as
flagged going in (no `helm` binary or Azure credentials on this machine).
- Phase 7 (6/6, implemented, unit-tested — 78 new unit tests, 237 total, passing —
and partially live-verified: real Alembic migration + Docker volume create/delete +
reconciler entrypoint against the real Postgres/Docker daemon, plus the persistent-
workspace mount/ownership-fix mechanism isolated from the one blocking host issue)
`PoolManager` (Postgres-backed claim ledger, not Redis — a deliberate design decision,
see Phase 7's own section), weight-class segregation (an in-process semaphore for
`heavy` on `local`, real `nodeSelector`/`tolerations` on `aks-prod`), warm-pool
claim/release wiring into `execute()`/`create_sandbox()`, persistent workspaces
(Docker named volumes, a per-workspace long-lived Kubernetes namespace+PVC), workspace
archive/purge retention (`WorkspaceService.sweep_retention()`), and the reconciler
(`app/reconciler/loop.py`, runnable standalone) are all real and unit-tested. The same
snap-Docker/AppArmor `no-new-privileges` host limitation Phase 6 found and left
unresolved blocked full end-to-end live verification of anything that actually starts
a sandbox container — confirmed to reproduce identically for this phase's own new
code paths, isolated and worked around piece-by-piece instead (see Phase 7's "Live
verification" and "Known environment limitation" sections). One bug found (a
mocked-client unit test caught a fresh-vs-reused-namespace logic error in
`KubernetesProvisioner.acquire()`'s persistent path before it ever reached a live
cluster) — see Phase 7's "Bugs found and fixed during live verification".
- Phase 8 (6/6, implemented, unit-tested — 284 total tests passing — and
live-verified against real Postgres/HTTP, including the doc §13 exit criterion's
blocking half end to end)
`BillingService` dispatches to `CreditBillingStrategy` (hard pre-authorization
against a wallet balance, real-time deduction) or `PayAsYouGoBillingStrategy`
(advisory spend-cap only, invoice-draft `settle()`), both wired into
`SandboxService.execute()`/`create_sandbox()`/`destroy_sandbox()` and three admin
endpoints (`PATCH .../billing`, `POST .../pricing-rules`, `POST .../credit`). Opt-in
via `billing.enabled` (false by default in both env profiles — see Phase 8's own
section for why) so existing behavior is unchanged unless explicitly turned on.
Initial pass (262 tests) live-verified `BillingService` directly against real
Postgres and, over real HTTP with billing enabled, the blocking half of doc §13's
exit criterion (`POST /v1/execute` returned a real 429 for an unfunded wallet, then
passed cleanly once funded) — the successful-run half stopped at the same
pre-existing snap-Docker/AppArmor host limitation Phases 6/7 already documented,
unrelated to this phase's own code. A same-session follow-up (prompted by an
explicit "was this a real constraint, or deferred scope?" question) then closed
every "known scope boundary" that wasn't a genuine blocker: `BillingService.
adjust_credit()` + `POST .../credit` (wallet top-up), `destroy_sandbox()` now bills
a non-ephemeral sandbox's real lifetime (closing `create_sandbox()`'s own gap and,
for free, TTL-reaped sandboxes too, since the reconciler already routes through
`destroy_sandbox()`), and a new reconciler job prices `storage_gb_day` for active
workspaces — plus a net-new, not-in-doc-§13 credit/overusage *request* workflow
(`credit_requests` table + migration, self-service `POST/GET
/v1/billing/credit-requests`, admin `GET`/`PATCH /v1/admin/credit-requests`) so a
blocked tenant has a real path forward besides an admin acting out of band. Two
real bugs found and fixed during this follow-up (a `Decimal`/`float` arithmetic
crash in `adjust_credit()`, and `destroy_sandbox()` not threading the reconciler's
own tick timestamp through) — see Phase 8's own "Follow-up" section for full detail
on both passes.
- Phase 9 and the cross-cutting section remain 0% started.

---

## Phase 0 — Foundations (21/21)

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
- [x] A generic non-root **base image/component** (`category: base`) that a
      `SandboxTemplate.spec.base.ref` could point to — `components/base/component.yaml`
      + `Dockerfile` (Debian-slim, uid 10001, bash/coreutils/git), plus the first real
      `SandboxTemplate` YAML (`templates/base-dev-lab.yaml`), added alongside Phase 2's
      template-composition work since a template needs *some* base to point at
- [x] OIDC/JWT session auth for standalone human users (doc §11) — **closed in Phase
      9b**, which needed it to make a browser UI possible at all:
      `app/services/auth_service.py` validates an IdP token against the issuer's JWKS
      and exchanges it for a KubeSandbox HS256 session token
      (`POST /v1/auth/token`), accepted as `Authorization: Bearer` on every route and as
      `?access_token=` on the WS attach handshake. See Phase 9b for the full design and
      for what remains unverified (no real IdP was available this session).

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

**5th bug, found later during Phase 2 live verification (same file, same root cause
class):** Tmpfs mounts had no explicit `exec`, and Docker silently mounts an
unqualified tmpfs `noexec` — invisible for every Phase 1 component (interpreted
languages never execve anything out of `/workspace`/`/tmp`), but it surfaced the
moment Phase 2's `go` component was exercised live (`go run` compiles a fresh binary
into `$GOTMPDIR`/`$GOCACHE`, both under `/tmp`, then execve's it — "permission
denied", no other symptom). Confirmed live:
`docker run --rm --tmpfs /tmp:rw,nosuid,nodev,size=1g,uid=10001,gid=10001,mode=0755
debian:12-slim mount | grep ' /tmp '` shows `noexec` present even though it was never
requested. Fixed by adding `exec` explicitly to the Tmpfs mount-options string in
`app/provisioners/docker.py`. Doesn't weaken containment — the sandboxed non-root user
can already run arbitrary code via the language interpreter/compiler itself — but it
would have silently broken every future compiled-language component (Rust, Java/JDK,
.NET, …), not just Go, so this is a load-bearing fix, not a Go-specific one.

---

## Phase 2 — Registry, templates & entitlements (10/10)

- [x] `SandboxTemplate` composition/rendering into a multi-component spec —
      `app/services/template_render.py`'s `render_template()` merges a template's
      base + declared components (env vars, writable paths, weight class, resources)
      into one `SandboxSpec`; wired end-to-end into `SandboxService.execute()` and
      `POST /v1/execute` via a new optional `template` field (`language` then picks
      which of the template's `mainTool` components to run). Real cross-image
      merging (a template mixing components whose golden images genuinely differ)
      needs BuildManager (Phase 6) — `render_template()` detects that case and raises
      a clear error instead of silently picking one image; see "Known scope
      boundaries" below for the one case that *is* genuinely runnable today.
- [x] Additional language components beyond `python` — **Node.js** (with a variable-dump
      batch runner mirroring Python's contract, doc §5.3, though it can only capture
      top-level `var`/implicit-global bindings, not `let`/`const` — a real V8/vm-module
      constraint, documented in `runner.js`), **Go** (`go run`, no variable dump —
      compiled, no serializable final scope), and **Bash** (`bash {file}`, baked into
      the shared `base` image). Doc §3.1 lists 13 languages total; Deno, Bun, Rust,
      Java/JDK, .NET, Ruby, PHP, Lua, R, Julia, TypeScript remain unimplemented —
      scoped down deliberately rather than shipping 13 shallow, untested stubs.
- [x] `GET /v1/components` (list/query registry, entitlement-filtered, optional
      `category` query param) — `app/api/v1/components.py`
- [x] `GET /v1/components/{name}` (all versions visible to the caller + the Component
      JSON Schema itself, so a manifest author can validate client-side before POSTing)
- [x] `POST /v1/components` (admin → public catalog under
      `components/<category-plural>/<name>/`; non-admin with a matching
      `publish_grant` → tenant-private catalog instead, see below — never the other
      way around)
- [x] `GET/POST /v1/templates` (list is entitlement-filtered by deriving visibility
      from every component the template references — there's no separate
      `template_entitlements` table in doc §3.6, only one for components; create
      follows the same admin-public/grant-gated-private split as components, using a
      synthetic `publish_grants.category = "template"`)
- [x] `EntitlementService` (`app/services/entitlement_service.py`) — reads/writes
      `component_entitlements`/`publish_grants` for real now; admin bypasses all of it;
      a private component/template's ownership is derived *structurally* from its
      registry key (`tenant/<tenant_id>/<name>@<version>`) rather than a new DB column,
      so there's nothing to keep in sync. `version_range` matching is deliberately
      simple (`"*"` or an exact version string) — the doc doesn't specify a full
      semver-range grammar.
- [x] `GET/PATCH /v1/admin/entitlements` — `app/api/v1/admin.py`
- [x] `GET/PATCH /v1/admin/publish-grants` — `app/api/v1/admin.py`
- [x] Private, tenant-namespaced component publishing (`tenant/<id>/<name>`) —
      lands on disk at `components/tenant/<tenant_id>/<name>/component.yaml`
      (templates: `templates/tenant/<tenant_id>/<name>.yaml`), loaded by the same
      `rglob` the public registry uses. This required fixing a latent bug in
      `Registry.latest_component`/`resolve_component_ref` (`app/extensions/loader.py`):
      they matched on `metadata.name`, which is *unqualified* even for a private
      component (schema-valid bare names only) — a bare lookup for a common name could
      have resolved to (and thus leaked the existence of) another tenant's private
      component of the same name. Fixed to match on the registry-key's name portion
      instead, which is qualified for private entries.

### Bugs found and fixed during adversarial review

An independent review pass over the diff (before commit) caught four real issues, none
of which the first round of unit tests exercised:

1. **Registering a new version of an existing component/template clobbered the
   previous version's file on disk.** `RegistryService`/`TemplateService` wrote to
   `<name>/component.yaml` / `<name>.yaml` — no version in the path — even though the
   registry fully supports multiple coexisting versions. Fixed by versioning the path
   itself (`<name>/<version>/component.yaml`, `<name>/<version>.yaml`); a restart would
   otherwise have silently lost the older version.
2. **`get_component_versions` sorted lexically, not semantically** (`"10.0"` sorted
   before `"9.0"`), unlike `Registry.latest_component`'s correct numeric-tuple sort.
   Fixed by exposing and reusing the same `version_sort_key` everywhere.
3. **The `ref` pattern in both JSON Schemas never actually permitted a
   tenant-qualified ref** (`tenant/<id>/<name>@<version>`) — `requires` and a
   template's `components[].ref` both used `^[a-z0-9][a-z0-9-]*@.+$`, which rejects
   any "/" before the "@". A component or template could never actually declare a
   dependency on a private, tenant-namespaced entry, silently undermining part of the
   private-publishing feature. Fixed by widening both patterns to
   `^(tenant/[a-z0-9-]+/)?[a-z0-9][a-z0-9-]*@.+$`.
4. **Publishing a component/template didn't check that the caller could actually see
   what it referenced.** `requires` (components) and `base`/`components` refs
   (templates) were only checked for *existence*, not *visibility* — a non-admin
   publisher could reference another tenant's private component by guessing its exact
   qualified ref. Fixed via a new `EntitlementService.is_ref_visible()`, enforced in
   both `RegistryService.register_component` and `TemplateService.create_template`
   for non-admin callers.

### Live verification (Phase 2)

Unlike the paragraph above might suggest from the implementation session alone, Phase 2
*was* subsequently live-verified end-to-end (same session, once Docker access was
available): all three new golden images were built —

```bash
docker build -t kubesandbox/node:20.15.0-slim components/languages/node
docker build -t kubesandbox/go:1.22.5-slim components/languages/go
docker build -t kubesandbox/base:1.0 components/base
```

— then `docker compose up -d`, `uv run alembic upgrade head`, and repeated
`POST /v1/execute` calls against the real stack for `python`, `node`, `go`, and `bash`
(ad-hoc) plus `template=base-dev-lab@1.0` (composed base+bash+git), all returning
correct bundled results. `GET /v1/components` and `GET /v1/templates` were hit live too.
This pass is what surfaced the tmpfs-`noexec` bug (see the "5th bug" entry above) —
exactly the kind of thing a `FakeProvisioner`-backed unit test structurally cannot catch.

### Known scope boundaries (Phase 2)

- **True multi-golden-image template composition is still Phase 6's job.**
  `render_template()` only succeeds when every `mainTool`-kind component in a template
  (base included) resolves to the *same* pre-baked image. The one genuinely
  runnable-today example, `templates/base-dev-lab.yaml` (base + bash + git), works
  because all three were deliberately baked into one shared image
  (`kubesandbox/base:1.0`) by hand — the same manual-build pattern Phase 1 used for
  `python`. A template mixing e.g. `python` and `node` (different images) is schema-valid
  and loads fine, but `render_template()` rejects *running* it with a clear error
  pointing at BuildManager, rather than silently dropping half the template.
- **Execution-time entitlement enforcement was not added.** `EntitlementService` gates
  the catalog *listing* and *publish* endpoints (the literal Phase 2 checklist wording)
  — it does not gate `POST /v1/execute`'s ad-hoc `language=`/`template=` resolution,
  which still goes straight through the `Registry` unfiltered, same as Phase 1. A tenant
  could execute against any public component regardless of whether they're entitled to
  *see* it in a catalog listing. Flagged here rather than silently left as a surprise;
  worth closing in a later hardening pass (Phase 9?) if that gap matters before prod.

---

## Phase 3 — Kubernetes + hardening (6/6 — fully live-verified)

- [x] `KubernetesProvisioner` (`app/provisioners/kubernetes.py`) — full `Provisioner`
      protocol via `kubernetes_asyncio`. Same "one already-running container, exec
      batch commands into it" shape as `DockerProvisioner`, but isolation is expressed
      as **namespace-per-sandbox**: `acquire()` creates a fresh Namespace holding a
      default-deny NetworkPolicy, a ResourceQuota, a LimitRange, and the sandbox Pod;
      `destroy()` deletes the whole namespace so nothing sandbox-scoped can leak.
      `app/main.py`'s `_build_provisioner` is now `async` and constructs it for
      `provisioner.backend == "kubernetes"` via `KubernetesProvisioner.create()`
      (kubeconfig file, in-cluster service-account token, or default kubeconfig
      discovery, in that priority order).
- [x] Pod security context rendering as real Kubernetes PodSpec fields —
      `runAsNonRoot`/`runAsUser`/`runAsGroup: 10001`, `fsGroup: 10001` (grants the
      sandbox uid write access to root-owned `emptyDir` volumes — the K8s-native
      equivalent of `DockerProvisioner`'s explicit tmpfs `uid`/`gid`/`mode`),
      `seccompProfile: RuntimeDefault`, `allowPrivilegeEscalation: false`,
      `capabilities.drop: ["ALL"]`, `readOnlyRootFilesystem`, `automountServiceAccountToken:
      false`, disk-backed (not `medium: Memory`) `emptyDir` for `/workspace`/`/tmp` —
      deliberately side-stepping the tmpfs-`noexec` class of bug Docker hit in Phase
      1/2 rather than reproducing it (see "Live verification" below for the
      Go-compiled-binary proof).
- [x] `deploy/manifests/base/` — `namespace.yaml` (the control plane's own
      `kubesandbox-system`, distinct from the per-sandbox namespaces
      `KubernetesProvisioner` creates dynamically), `rbac.yaml` (least-privilege
      `ServiceAccount`/`ClusterRole`/`ClusterRoleBinding` — namespaces/pods/pods-exec/
      networkpolicies/resourcequotas/limitranges, nothing broader), `networkpolicy.yaml`
      (default-deny for `kubesandbox-system`), `resourcequota.yaml`, `limitrange.yaml`,
      `runtimeclass.yaml` (`gvisor` / `runsc` handler) — all wired into one
      `kustomization.yaml`.
- [x] `deploy/overlays/local/` and `deploy/overlays/aks-prod/` Kustomize overlays —
      each layers environment-specific NetworkPolicy egress-allow rules on top of
      base's default-deny (doc §12: the allowlist is an overlay concern, never decided
      by the control plane at runtime); `aks-prod` additionally patches the
      `ResourceQuota` to prod scale. Both build cleanly with `kubectl kustomize` and
      were `kubectl apply -k`'d against a real cluster with zero errors.
- [x] gVisor/Kata `RuntimeClass` wiring — `KubernetesProvisioner` reads
      `provisioner.runtime_class` and sets `runtimeClassName` on every sandbox pod when
      configured (`aks-prod.yaml`: `gvisor`; `local.yaml`: `null`, so local pods never
      reference it even though the `RuntimeClass` object itself is always created by
      the base manifests — inert until a real gVisor-enabled node pool exists, which is
      cluster/infra-level and out of this repo's code, roadmap Phase 9).
- [x] `kind`-cluster-based Kubernetes-spec parity testing setup —
      `deploy/kind/kind-config.yaml` (pinned `kindest/node:v1.32.2`);
      `tests/integration/test_execute_kubernetes.py` mirrors
      `test_execute_docker.py`'s self-skip-if-unavailable pattern, checking the golden
      image is loaded into the kind node's containerd store (`crictl inspecti`, not
      just the host Docker daemon — kind nodes don't share the host's image cache).

### The stdin-EOF-over-K8s-exec discovery

Unlike `DockerProvisioner`'s raw hijacked TCP stream (a real half-close cleanly signals
"no more input" while the read side stays open), the Kubernetes exec API multiplexes
stdin/stdout/stderr/exit-status as byte-prefixed frames over **one** WebSocket
connection. Empirically confirmed against a live kind cluster (not guessed): with only
the default `v4.channel.k8s.io` subprotocol negotiated, a process blocked on stdin
(`cat`, or a batch runner's `input()`) never sees EOF — the exec session hangs until the
wall-clock timeout reaps it. Fix, also confirmed live: offer `v5.channel.k8s.io`
alongside v4 in the WebSocket handshake and send a control frame — byte `255`
(close-channel index) followed by the target channel index (`0` = stdin) — which closes
just that stream and delivers real EOF while stdout/stderr/exit-status keep flowing on
the same connection. Degrades safely on a server that only supports v4 (the frame is
silently ignored, same wall-clock-timeout fallback philosophy as Docker's
`_half_close_stdin`). Full reasoning and the empirical probe sequence that found this
are documented in `app/provisioners/kubernetes.py`'s module docstring.

### Bug found and fixed during live verification

**Kubernetes label *values* are far more restrictive than Docker's.** The first live run
of `POST /v1/execute` against a real kind cluster failed with a `422` from the API
server: `component_ref` labels like `"python@3.12.4"` (fine as an arbitrary Docker
label) fail Kubernetes' label-value regex (no `@`, no `/`). Fixed by moving
`spec.labels` to **annotations** (no charset restriction) on both the Namespace and the
Pod, keeping only the sandbox-id UUID as an actual label — exactly the kind of bug this
codebase's live-verification practice exists to catch, since a mocked API client would
never enforce the real server's validation rules.

### Live verification (Phase 3)

All against a real kind cluster (`kubesandbox-dev`, `kindest/node:v1.32.2`), not
mocks — golden images loaded via `kind load docker-image`:

- `POST /v1/execute` driven over real HTTP against the full FastAPI app (`APP_ENV=local`
  with `KUBESANDBOX_PROVISIONER__BACKEND=kubernetes` overriding just the provisioner)
  for `python`, including the stdin round-trip and variable dump.
- Direct `SandboxService` + `KubernetesProvisioner` runs for `node` (variable dump),
  `go` (compiled binary execve'd out of `/tmp` — the exact class of bug that broke
  `go run` under Docker's tmpfs in Phase 1/2, confirmed *not* to reproduce under K8s's
  disk-backed `emptyDir`), and the composed `base-dev-lab@1.0` template (`bash`/`git`).
- A held-open sandbox pod was inspected directly: `kubectl get pod ... -o jsonpath`
  confirmed the security context was accepted by the API server exactly as sent
  (`runAsNonRoot`, uid/gid 10001, `seccompProfile: RuntimeDefault`,
  `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`,
  `readOnlyRootFilesystem: true`); `kubectl exec ... id` confirmed the process actually
  runs as uid 10001, not root; `touch /etc/...` failed (read-only rootfs) while
  `touch /workspace/...` succeeded (writable via `fsGroup`); and — the actual
  containment claim, not just accepted YAML — the default-deny NetworkPolicy was
  confirmed to really block egress from inside that live pod, both to an external IP
  and to the Kubernetes API server itself.
- Namespace teardown confirmed idempotent and leak-free across every run
  (`kubectl get namespaces` polled to empty after each test), with graceful-GC-lag
  handled by polling rather than asserting instantly — Kubernetes namespace deletion is
  asynchronous and its exact timing varies with API server/etcd load, confirmed live
  when a short fixed timeout flaked under a full-suite test run's create/delete churn.

### Known scope boundaries (Phase 3)

- **No real gVisor/Kata execution.** The `RuntimeClass` object and the provisioner's
  `runtimeClassName` wiring are real and live-tested for *absence* (local pods never
  set it), but there is no gVisor-enabled node anywhere this can run against — that
  requires real AKS infrastructure (cluster/infra-level, roadmap Phase 9), so the
  actual kernel-isolation behavior itself is untested, honestly flagged rather than
  assumed.
- **NetworkPolicy enforcement depends on the cluster's CNI**, not on anything this repo
  controls — confirmed live that kind's default CNI on this node image *does* enforce
  it, but that's a property of the cluster, not a guarantee `KubernetesProvisioner`
  itself can make. `aks-prod`'s actual CNI/network-policy engine choice is a deploy-time
  decision outside this repo's code.
- **Interactive attach is still Phase 4** — `KubernetesProvisioner.attach()` raises
  `NotImplementedError`, same placeholder shape as `DockerProvisioner.attach()`.
- **Execution-time entitlement enforcement gap from Phase 2 is unchanged** — still
  flagged there, not re-litigated here.

---

## Phase 4 — Interactive PTY (7/7, implemented, unit-tested, and live-verified)

- [x] `WS /v1/sandboxes/{id}/attach` — `app/streaming/ws_gateway.py`. Auth via a
      `?api_key=` query param (browsers can't set custom WS headers on a handshake),
      resolved through the same `_resolve_principal` helper `X-API-Key` uses
      (`app/api/deps.py`), honoring `auth.disabled` for local dev.
- [x] `PTYStream` implementation in `DockerProvisioner` — `attach()` execs
      `dtach -A /tmp/.kubesandbox-attach.sock -e none -z /bin/bash` with `tty=True`;
      `DockerPTYStream` wraps the aiodocker `Exec`/`Stream` pair, resize via
      `Exec.resize(h=,w=)`. (Originally `tmux`; see "Bugs found" below for why that
      doesn't work in these containers at all.)
- [x] `PTYStream` implementation in `KubernetesProvisioner` — same dtach invocation
      over the channel-framed exec WebSocket `exec_batch` already uses, but held open
      for the session's lifetime; resize sends `{"Width":,"Height":}` JSON on
      `RESIZE_CHANNEL` (4).
- [x] `resize`/`signal` client-frame handling — `app/streaming/pty_protocol.py`
      defines the JSON+base64 wire frames (`stdin`/`resize`/`signal` from the client,
      `stdout`/`exit` from the server). **Signals are not a separate transport
      primitive on either backend** — a PTY has no "send signal" verb; the WS gateway
      maps `signal: SIGINT|SIGQUIT|SIGTSTP` to the matching control byte (Ctrl-C/
      Ctrl-\/Ctrl-Z) and writes it via `write_stdin`, exactly how `docker exec -t`/
      `kubectl exec -t`/every real terminal deliver signals. `SIGTERM`/`SIGKILL` are
      not representable this way — a documented limitation, not an oversight; a caller
      wanting a hard kill destroys the sandbox instead.
- [x] Single-viewer enforcement (409 on a concurrent second attach attempt) — one
      Redis key (`attach:{sandbox_id}`) holds the attached identity with a
      heartbeat-refreshed TTL; a *different* identity attaching while it's live is
      rejected with a real HTTP 409 raised before `websocket.accept()` (FastAPI sends
      this as a genuine HTTP error during the handshake, not a WS close code).
- [x] Reattach-after-disconnect grace window — falls out of the same lock for free:
      on disconnect the key is simply left to expire naturally (no separate
      bookkeeping) rather than deleted; the *same* identity reconnecting before it
      lapses is let straight back in. Reattaching resumes the *same* live shell (cwd,
      env, running jobs) via dtach's `-A` (attach-or-create) flag — required adding
      `dtach` to every interactively-used golden image
      (`components/base`, `components/languages/{python,node,go}`).
- [x] `GET/PUT /v1/sandboxes/{id}/files`, `GET /v1/sandboxes/{id}/tree` —
      `Provisioner.get_file`/`list_tree` added to both backends (Docker via `exec`
      `cat`/`find -printf`, reusing/generalizing the existing variable-dump capture
      helper; Kubernetes via a new byte-preserving `_run_exec_raw` so a binary
      download isn't corrupted by `_run_exec`'s lossy UTF-8 decode). Bounded to
      `/workspace` with no `..` escape, validated once at the API layer.

### Prerequisite this phase needed but the checklist didn't spell out

`attach()`/`.../runs`/the file APIs all need an addressable sandbox that outlives a
single request — today only `POST /v1/execute`'s ephemeral acquire→run→destroy existed.
Added the doc §17 CRUD surface `SandboxService` was missing:
`create_sandbox`/`get_sandbox`/`get_sandbox_status`/`destroy_sandbox`/`run_in_sandbox`/
`open_pty`/`get_file`/`put_file`/`list_tree`, and `app/api/v1/sandboxes.py`
(`POST /v1/sandboxes`, `GET/DELETE /v1/sandboxes/{id}`, `POST .../runs`, plus the file
routes above). Same reasoning as Phase 0 quietly adding the `base` component alongside
Phase 2's template work — a hard dependency the phase can't function without.

Also added, not on the original checklist but load-bearing for the above:
`app/persistence/redis.py` (doc §10.1 describes Redis for exactly this — session/
attach registry — but no client existed yet), and a full OpenAPI/Swagger documentation
pass across every HTTP route (summaries, per-field descriptions, centralized tag
metadata, Schemas section hidden in Swagger UI) since the sandbox lifecycle surface
more than doubled the API in this phase.

### Bugs found and fixed during live verification (relayed hand-off)

None of these were visible to the unit tests (which use `FakeProvisioner`/mocked K8s
clients) — both only surfaced once `attach()` ran against a real container. Live
verification for Phase 4 was a **relayed** loop (the assistant has no direct
Docker/kind daemon access this session — see "Known scope boundaries" below), but the
bugs found and the fixes needed are identical to a direct session; only the mechanism
of discovery differs.

1. **tmux's fallback shell resolves to `nologin`, killing the session instantly.**
   The first `attach()` implementation ran `tmux new-session -A -s ks-main` with no
   explicit pane command, so tmux fell back to the current user's `/etc/passwd` shell
   entry — every sandbox user is created with `--shell /usr/sbin/nologin` (doc §6 "no
   root, no admin" hardening, Phase 0/1). `nologin` exits immediately, killing the
   pane and, since it's the only one, tearing the whole tmux session/server down with
   it. Confirmed live: the client got tmux's own `[exited]` message and `exit_code: 0`
   (a clean tmux shutdown, not a crash) instead of a prompt. First fix attempt: name
   the shell explicitly — `tmux new-session -A -s ks-main /bin/bash` — bypassing
   `/etc/passwd`. This also surfaced that `python:3.12-slim`/`node:20-slim` don't ship
   `bash` by default (only `components/base`'s Dockerfile had ever installed it).
2. **tmux itself doesn't work in these containers at all, independent of the shell
   fix above.** Even with the explicit `/bin/bash` fix, `attach()` still immediately
   returned `[exited]`/`exit_code: 0`. Debugged live by testing tmux directly via
   `docker exec -it` (bypassing the app entirely) with `tmux -vv` verbose logging:
   `spawn_pane: moving pane to new cgroup failed: failed to connect to session bus:
   No medium found` — tmux >=3.4 tries to move every new pane into its own cgroup via
   a systemd D-Bus call, and this is apparently fatal to the pane in this build, not
   merely a warning. Confirmed conclusively with `tmux new-session -d` (fully
   detached, zero clients involved at all): the server still had "0 sessions" and had
   exited immediately after, ruling out anything client/attach-specific — the pane's
   process itself never survives. Installing a dbus/systemd stack just to satisfy
   tmux inside an intentionally minimal, non-root sandbox container would be a much
   bigger (and security-relevant) change than swapping tools, so **tmux was replaced
   with `dtach`** — a small, single-purpose attach/detach wrapper around one pty with
   no cgroup/systemd/dbus dependency at all: `dtach -A
   /tmp/.kubesandbox-attach.sock -e none -z /bin/bash` (`-e none` disables dtach's own
   detach-escape-key handling, since detach should only ever happen via the WS
   connection actually dropping, not an in-band control byte that could collide with
   `signal: SIGQUIT`'s Ctrl-\; `-z` passes Ctrl-Z straight through to the shell
   instead of dtach swallowing it as a host-side suspend key).

Exactly the kind of bug this project's live-verification practice exists to catch —
both depend on real `/etc/passwd` entries, a real container's init system (or lack of
one), and a terminal multiplexer's actual process-spawning behavior, none of which a
`FakeProvisioner`-backed unit test can exercise.

### Live verification (Phase 4)

Run via a **relayed hand-off loop**, not directly by the assistant — this session has
no direct Docker/kind daemon access (the sandboxed shell gets `permission denied` on
the Docker socket), so exact commands were handed to the user, output pasted back,
bugs fixed and re-handed-off. Same end result as Phases 1–3's direct live
verification, just relayed — and it's exactly this practice that caught both bugs
above, neither of which a `FakeProvisioner`-backed unit test could have. Confirmed
against the real `python` golden image, rebuilt with `dtach`:

- `POST /v1/sandboxes` (create) → `GET /v1/sandboxes/{id}` (status) → `POST
  .../runs` (batch run against the warm sandbox, confirmed it does **not** get
  destroyed afterward, unlike `/v1/execute`) → `DELETE` (destroy).
- `PUT`/`GET /v1/sandboxes/{id}/files` and `GET /v1/sandboxes/{id}/tree` — round-
  tripped a file and confirmed the tree listing matched (this is also what confirmed
  `find -printf` works as expected in the golden image, not just assumed).
- **Interactive attach + reattach — the core of this phase — confirmed working
  end to end** via `scripts/ws_attach_demo.py`: attached, ran `pwd`/`export
  FOO=bar`/`cd /tmp`, disconnected (Ctrl-D on the client), reattached with a fresh
  client process, and confirmed `pwd` → `/tmp` and `echo $FOO` → `bar` — proving the
  `dtach`-backed session genuinely persisted server-side across a disconnect, not
  just that a new shell happened to start.

### Known scope boundaries (Phase 4)

- **Single-viewer 409 and explicit destroy-cleanup weren't separately re-confirmed
  live this round** (attach/reattach — the one behavior that could only be proven
  against a real container — took priority and consumed the verification pass). Both
  are lower-risk: the 409/reattach-grace logic is pure Redis-based application code
  with 10 dedicated unit tests (`tests/unit/test_ws_gateway.py`) and no
  Docker/tmux/dtach-specific behavior to surprise it, unlike the attach mechanism
  itself.
- **File upload is UTF-8 text only** (matching `put_files()`'s existing str-content
  contract, the same one batch execution's `files` argument uses) — a binary upload is
  rejected with a 400 rather than silently corrupted. Download is binary-safe.
- **The live-verification session's kind cluster was incidentally destroyed** by an
  overly-broad `docker rm -f --filter name=kubesandbox-` cleanup command mid-session
  (it also matched the kind node container, `kubesandbox-dev-control-plane`) —
  unrelated to any Phase 4 code; flagged here since Phase 3/5's Kubernetes-path live
  verification will need `kind delete cluster && kind create cluster` to rebuild it
  before it can run again. The `docker-compose` infra (Postgres/Redis/MinIO/registry)
  was also caught by the same filter but recovered cleanly — those containers attach
  to named volumes that `docker rm` (without `-v`) never touches, so `docker compose
  up -d` recreated them with all data intact.

---

## Phase 5 — Database add-ons (6/6, implemented, unit-tested, and fully live-verified)

- [x] `components/databases/postgresql/component.yaml` + `hooks.py` — `postgres:16-alpine`
      sidecar; `on_provision` creates a non-superuser `sandbox_user` role and a
      per-sandbox database via `psql` (run as the bootstrap `postgres` superuser over
      the local unix socket, which the official image trusts without a password),
      applies the manifest's declared `grants`/`statementTimeout`/`maxConnections`.
- [x] `components/databases/mysql/component.yaml` + `hooks.py` — `mysql:8.4` sidecar;
      same shape as Postgres but via the `mysql` CLI (root, authenticated with the
      bootstrap `MYSQL_ROOT_PASSWORD` — MySQL, unlike Postgres, always requires a
      password even over the local socket), no `FILE`/`SUPER`/`PROCESS`/`RELOAD`.
- [x] `components/databases/redis/component.yaml` + `hooks.py` — `redis:7-alpine`
      sidecar, distinct from the Redis instance in `docker-compose.yml` (that one is
      control-plane infra — the session/attach-lock registry — not a sandbox add-on).
      Redis has no SQL-style role/grant model, so `on_provision` applies the
      "no superuser" principle via `ACL SETUSER` instead: creates the scoped user with
      the manifest's declared ACL rule tokens, then disables the (until then
      unauthenticated-by-default) `default` user.
- [x] Multi-container (main + sidecar) composition in both provisioners —
      `DockerProvisioner` creates each sidecar as its own container with
      `network_mode: container:<main>` (shares main's already-`none` network
      namespace, so main<->sidecar localhost reachability doesn't weaken the existing
      no-external-connectivity guarantee) and per-sidecar tmpfs mounts owned by that
      sidecar's own uid (not the sandbox's); `KubernetesProvisioner` adds one
      `V1Container` per sidecar to the same Pod (same-pod containers already share a
      network namespace, so no NetworkPolicy change was needed), with a
      per-container `securityContext` overriding the pod-wide sandbox uid and a
      `readinessProbe` built from the manifest's `healthCheck`. The namespace's
      `ResourceQuota`/`LimitRange` were extended to account for sidecar resources on
      top of main's (a namespace-total quota sized to main alone would reject pod
      admission the moment a sidecar was attached).
- [x] A concrete `ComponentHook` Python module — `app/extensions/hooks.py` defines the
      `Protocol` (doc §3.5) and a `load_hook()` loader; `SandboxService` drives
      `on_provision` for real right after `acquire()` succeeds, and `on_teardown`
      (a documented no-op in all three DB hooks today — whole-sandbox teardown
      already wipes everything they touch) right before `destroy()`.
      `validate`/`mutate_pod_spec` are part of the Protocol but not wired to any
      caller — see "Known scope boundaries" below.
- [x] DB-scoped-role provisioning logic — non-superuser role/database (or ACL user,
      for Redis) created by each hook, scoped grants pulled straight from the
      manifest, `maxConnections` enforced on all three, `statementTimeout` enforced
      for Postgres and approximated for MySQL (see "Known scope boundaries").

### Prerequisites this phase needed but the checklist didn't spell out

- `SidecarSpec` + `SandboxHandle.sidecar_refs` (`app/domain/execution.py`) and
  `Provisioner.exec_in` (`app/provisioners/base.py`) — the Protocol had no way to
  address "the sidecar, not main" at all before this phase.
- A new `sandboxes.sidecar_refs` DB column (Alembic revision `abcaabfd20d0`) —
  `destroy_sandbox()` runs in a separate request from `create_sandbox()`/`execute()`,
  so Docker's opaque sidecar container ids have to be persisted somewhere or they're
  unrecoverable at teardown time (Kubernetes' sidecar refs happen to just be the
  container name, but the column stays backend-agnostic either way).
- `app/services/credentials.py` (`generate_db_credentials`) — the scoped role's
  password and the sidecar's own bootstrap admin password have to be generated
  *before* `acquire()`: main's `DATABASE_URL` env var is baked into the container at
  creation time, but the role it points at is only actually created afterward, by the
  hook, so both sides need the exact same generated values threaded through.
- `ComponentRuntime.uid` and `DatabaseAccess.adminPasswordEnv` — two new
  component-manifest fields (`schemas/component.schema.json` +
  `app/domain/manifests.py`) neither the architecture doc nor the original schema had
  a place for: the OS uid/gid a sidecar's own image runs as (needed for tmpfs/emptyDir
  ownership and the Kubernetes per-container `securityContext`), and which env var
  name a sidecar's own bootstrap admin password is injected under (differs per DB
  engine — `POSTGRES_PASSWORD` vs `MYSQL_ROOT_PASSWORD`; Redis has no such mechanism
  at all).
- Fixed a latent bug in `render_template`'s pre-existing env/writable-path merge loop:
  it iterated *every* component (main + sidecar together), which would have leaked a
  DB sidecar's own env (including its bootstrap admin password, once a sidecar
  actually existed) and writable paths (e.g. its data directory) onto main's spec.
  Never triggered before this phase — no template had ever composed a non-`mainTool`
  component — but fixed as part of materializing sidecars for real.

### Bugs found and fixed during live verification (relayed hand-off)

Same relayed pattern as Phase 4 (this session has no direct Docker/kind daemon
access): commands handed to the user, output pasted back, bug fixed, re-handed-off.

1. **Dropping ALL capabilities on the sidecar container broke the official DB
   images' own root-bootstrap-then-drop-privileges pattern.** The first `postgresql`
   sidecar attempt failed with `sidecar 'postgresql' did not become healthy within
   30s`, and the container's own logs showed `chown: /var/lib/postgresql/data:
   Operation not permitted` / `chmod: ... Operation not permitted` — even though the
   process is uid 0 (root). Official Postgres/MySQL/Redis images start as root PID 1,
   `chown`/`chmod` their data directory to their own service user, then `gosu` (a
   direct `setuid()`/`setgid()` call, not a setuid-bit executable, so unaffected by
   `no-new-privileges`) to actually run the server as that non-root user — but Linux
   capabilities gate root's OWN powers too, not just non-root users, so `CapDrop:
   ["ALL"]` (this container's original, more-hardened-than-necessary posture, copied
   from main's) left root unable to `chown`/`chmod`/`setuid` at all. The subsequent
   `OCI runtime exec failed: ... procReady not received` health-check errors were a
   downstream symptom, not a separate bug — the container's own PID 1 had already
   crashed from the failed `chown`. **Fix:** `CapAdd: ["CHOWN", "FOWNER",
   "DAC_OVERRIDE", "SETUID", "SETGID"]` alongside the existing `CapDrop: ["ALL"]` —
   restores exactly the 5 capabilities this bootstrap pattern needs, nothing broader
   (`app/provisioners/docker.py::_create_sidecar`). An attempt to confirm each
   image's uid via `docker run --rm <image> id` (as originally planned) turned out to
   be a red herring: overriding `CMD` with a bare `id` bypasses the entrypoint script's
   user-switch logic entirely (it only triggers for its own recognized subcommands),
   so it always reports root regardless of what uid the actual server process runs
   as — this doesn't matter for correctness now anyway, since the entrypoint
   `chown`s the (tmpfs-premounted) data directory to whatever uid it actually expects
   once it has the capability to do so, independent of `SidecarSpec.uid`'s guessed
   value.
2. **A sidecar that starts but never becomes healthy leaked its container.**
   `DockerProvisioner.acquire()`'s cleanup-on-failure path only removes containers
   recorded in the `sidecar_refs` dict it builds while looping over `spec.sidecars`
   — but the original code only wrote a sidecar's id into that dict *after*
   `_create_sidecar()` returned successfully, and `_create_sidecar()` itself awaited
   the health-check (and raised on timeout) before returning. So a sidecar that
   crashed during bug #1 above never got its id recorded at all, and the except
   block's `for sidecar_id in sidecar_refs.values(): ...` cleanup loop had nothing to
   remove it with — confirmed live: two crashed `-postgresql` containers survived
   two separate cleanup attempts and had to be removed by hand. **Fix:** moved the
   health-check call out of `_create_sidecar()` (which now just creates the
   container and returns it) and into `acquire()`'s loop, recording the container id
   into `sidecar_refs` immediately after creation succeeds and *before* awaiting the
   health check — so a container that exists but never turns healthy is still found
   by the cleanup loop either way.
3. **MySQL's healthcheck reported healthy before root's real password was active,
   racing `on_provision`'s very first connection.** The official `mysql` image's own
   entrypoint briefly runs a temporary, no-password bootstrap `mysqld` (over the same
   local socket `"localhost"` resolves to) to apply `MYSQL_ROOT_PASSWORD`, before the
   real, password-protected instance takes over. The original healthcheck,
   `mysqladmin ping -h localhost` (run as OS root inside the exec, so implicitly
   `-uroot` with no password), happily reported healthy against that *temporary*
   instance — well before root's real password was actually active — so
   `on_provision`'s first `mysql -uroot -p"$MYSQL_ROOT_PASSWORD"` connection failed
   live with `ERROR 1045 (28000): Access denied for user 'root'@'localhost' (using
   password: YES)`. **Fix:** changed the healthcheck itself to authenticate with the
   real password (`mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e 'SELECT 1'`,
   `components/databases/mysql/component.yaml`) — passing *that* check is the actual
   precondition `on_provision` needs, not merely "mysqld responds to something".
   Postgres's `pg_isready` healthcheck doesn't hit the analogous race: it checks
   whether the postmaster is accepting connections at all, without needing to
   authenticate as a specific password-protected role.
4. **The 30s sidecar health-check timeout was routinely too tight for MySQL under a
   constrained cpu limit.** After fixing bug #3, the mysql sidecar still failed with
   `did not become healthy within 30s` — but with real diagnostic output this time
   (`Can't connect to local MySQL server through socket ... (2)`), which turned out to
   be "still starting," not crashed. Reproduced directly via `docker run` bypassing
   the app entirely (bug #1/#2's proven debugging approach): with no resource limits,
   `mysqld` was ready for connections in under 2 seconds; with the exact `--cpus 0.5
   --memory 512m --pids-limit 128` this sidecar actually runs under, InnoDB
   initialization alone took over 10 seconds and total startup ran to ~36 seconds —
   past the 30s window. **Fix:** raised `_SIDECAR_HEALTH_TIMEOUT_SECONDS` from 30 to
   90 (`app/provisioners/docker.py`) — matches, with margin, the ~60s Kubernetes'
   own `readinessProbe` settings already allowed for the identical sidecars
   (`initial_delay_seconds=1, period_seconds=2, failure_threshold=30`), which had
   simply never been exercised long enough to notice Docker's shorter window was
   inconsistent with it.

### Live verification (Phase 5)

Run via the same relayed hand-off loop as Phase 4 (this session has no direct Docker
daemon access). Confirmed end to end against all three real sidecar images, each via
its own single-DB companion template (`templates/python-{postgres,mysql,redis}-lab.yaml`):

- `POST /v1/sandboxes` with each template → both containers (main + the DB sidecar)
  came up together every time, the sidecar reaching a healthy state via the exec-based
  health-check poll.
- `POST .../runs` reading `os.environ["DATABASE_URL"]` from inside main returned a
  correctly-formed, protocol-appropriate DSN each time — confirming the
  credential-generation/DSN-injection path end to end for all three.
- **Postgres**: connected directly to the sidecar as `sandbox_user` and confirmed
  `select current_user, current_database()` → `sandbox_user`/`sandbox`, then
  `CREATE ROLE escalated SUPERUSER` rejected with `permission denied to create role`.
- **MySQL**: same shape — `SELECT CURRENT_USER(), DATABASE()` → `sandbox_user@%`/
  `sandbox`, then `CREATE USER 'escalated'@'%' ...` rejected with
  `Access denied; you need (at least one of) the CREATE USER privilege(s)`.
- **Redis**: `PING`/`ACL WHOAMI`/`SET`+`GET` all worked as `sandbox_user`; `FLUSHALL`
  rejected with `NOPERM User sandbox_user has no permissions to run the 'flushall'
  command`; and — confirming `on_provision` actually disabled the default user, not
  just created a scoped one — an unauthenticated `redis-cli PING` against the same
  sidecar was rejected with `NOAUTH Authentication required`.
- `DELETE /v1/sandboxes/{id}` cleanly removed both containers every time, for all
  three templates.

Four real bugs surfaced and were fixed along the way — see "Bugs found and fixed
during live verification" above; none would have been visible to
`FakeProvisioner`-backed unit tests, the same story as every prior phase's live pass.
Notably, MySQL alone needed three follow-up fixes (a healthcheck race, an
output-suppressing healthcheck command that hid the real error, and a too-tight
timeout) before it passed — Postgres and Redis each passed on their first live
attempt once the two provisioner-level bugs (capability restoration, cleanup
ordering) were fixed.

### Known scope boundaries (Phase 5)

- **`SidecarSpec.uid`'s exact value (999 for all three images) remains formally
  unconfirmed** against a live container (`docker run --rm <image> id` isn't a
  reliable way to check this — see bug #1's entry) — but, per that bug's fix, this no
  longer blocks correct behavior on Docker: the official images' own entrypoint
  chowns the data directory to whatever uid it actually needs (now that the container
  has the capability to do so), independent of what this field guessed. It's still
  relevant for Kubernetes' per-container `securityContext.runAsUser`, which — unlike
  Docker — forces the container to start AS that uid directly rather than as root, so
  it's worth confirming precisely whenever the Kubernetes path is live-verified (kind
  cluster currently torn down, see Phase 4's "Known scope boundaries" — non-urgent
  until then).
- **Kubernetes' sidecar security model hasn't been live-verified at all yet**, and
  is architecturally different from Docker's fix above: it forces
  `runAsUser: sidecar.uid` directly (skipping the root-bootstrap phase entirely) and
  relies on the pod-wide `fsGroup` making the emptyDir volume group-writable for a
  supplemental-group member, rather than the image's own `chown`+`gosu` pattern. This
  is plausible (K8s' fsGroup mechanism exists for exactly this reason) but genuinely
  untested — flagged here rather than assumed correct by analogy with the Docker fix.
- **`maxDbSizeMB` is declared in the Postgres/MySQL manifests but not enforced** —
  neither engine has a native per-database size cap; enforcing it needs an extension
  (e.g. `pg_quota`) or an external reconciler polling actual size, out of scope here.
- **MySQL's `statementTimeout` is approximated, not a true equivalent** —
  `SET GLOBAL max_execution_time` is the closest MySQL analog to Postgres' per-role
  `statement_timeout` (MySQL has no persistent per-user form), safe only because each
  `mysql` sidecar is single-tenant — one instance per sandbox — so a `GLOBAL` setting
  only ever affects that one sandbox.
- **Redis's DSN path segment is `0`** (a numeric db index), not
  `credentials.database`'s SQL-style logical name — Redis addresses databases by
  index, not name; `template_render._build_dsn` special-cases this one protocol.
- **`validate`/`mutate_pod_spec` on `ComponentHook` are unwired** — part of doc §3.5's
  full Protocol, but nothing in this phase's actual requirements needs them; wiring
  them speculatively would be exactly the kind of building-for-hypothetical-future-
  requirements this project avoids elsewhere.
- **No single template composes more than one DB sidecar** — each of the three
  companion templates (`python-{postgres,mysql,redis}-lab.yaml`) composes exactly one
  DB component alongside `python`; all three declare `dsnEnv: "DATABASE_URL"`, so
  composing two DB sidecars into the same template would collide on that env var name
  (a manifest-authoring conflict, not a code bug) — not attempted, since nothing
  currently needs it.

---

## Phase 6 — Build system & golden images (8/8, implemented and unit-tested; live verification prepared, not yet run)

- [x] `BuildManager` service (`app/services/build_manager.py`) — `trigger_build()`
      creates a `pending` `Build` row and returns immediately (mirroring `/v1/execute`'s
      sync/async duality, doc §5.1, but mandatory here since a real image build can
      take minutes); `run_build()` does the actual work as a FastAPI `BackgroundTask`
      (not a bare `asyncio.create_task`, which risks GC if unreferenced — `BackgroundTasks`
      is Starlette's supported mechanism for exactly this), using its own DB session
      since the triggering request's is already closed by the time it runs. Dispatches
      to a fixed, built-in strategy map keyed by `source.type` — no plugin loading
      needed, unlike `ComponentHook`, since these four are internal, not user-pluggable
      per component. Duplicate-build suppression (an in-flight `pending`/`running`
      build for the same component is returned instead of started twice). Gating
      mirrors `RegistryService.register_component`'s publish trust boundary: admin can
      build any public component; a non-admin only their own tenant-private one.
      `hydrate_built_images()` rehydrates `Registry.built_images` from the latest
      successful `Build` row per component at startup (`app/main.py`'s lifespan) — a
      control-plane restart doesn't forget a previously-built component.
- [x] `BuildStrategy` implementation: `dockerfile` (`app/build/strategies/dockerfile.py`)
      — builds against the **local Docker daemon via aiodocker** (doc §8.1's documented
      `local` fallback), not Kaniko/BuildKit-in-Kubernetes — see "Known scope
      boundaries" below for why the `aks-prod` Kaniko path is deliberately not built
      this phase. `_tar_context()` (pure, unit-tested directly) tars the build context;
      `build_image_from_dockerfile()` is a shared module-level function `compose.py`
      also calls for its own per-service builds.
- [x] `BuildStrategy` implementation: `compose` (`app/build/strategies/compose.py`) —
      a kompose-style translator scoped to what this phase needs: parses a
      `docker-compose.yaml`'s declared services (pure `parse_compose_services()`/
      `select_primary_service()`, unit-tested without touching Docker), builds/tags
      each service with a `build:` context via the same `build_image_from_dockerfile`
      helper `dockerfile.py` uses, and returns the service matching the component's own
      name (or the first declared service) as the primary `Artifact`; other built
      services are recorded in `Artifact.metadata["services"]`.
- [x] `BuildStrategy` implementation: `pipeline` (`app/build/strategies/pipeline.py`) —
      runs declared `steps` in order via an **injectable step-runner** (defaults to
      `asyncio.create_subprocess_shell`; injectable so unit tests substitute a fake
      recorder, the same "swap the I/O boundary" pattern `FakeProvisioner` uses for
      `SandboxService`), failing fast on the first non-zero exit, with `$IMAGE`/
      `$COMPONENT_NAME`/`$COMPONENT_VERSION` available in each step's environment.
      Real caching against the new `ObjectStorageProvider`: computes doc's own example
      cache key (`"{name}-{version}"`), and a cache hit skips re-running steps
      entirely — unit-tested by asserting a second `build()` call doesn't re-invoke the
      step runner. The final image is packaged via the same `build_image_from_dockerfile`
      helper (see "Known scope boundaries" for why, not a raw `kaniko`/`docker build`
      shell-out like doc's own example step).
- [x] `BuildStrategy` implementation: `helm` (`app/build/strategies/helm.py`) — renders
      a chart via `helm template` (subprocess), uploads the rendered manifest to the
      new `ObjectStorageProvider` as a `kind: "manifest"` `Artifact`. Fails loudly with
      a clear `BuildError` if the `helm` binary isn't on `PATH`, matching doc's
      cloud-stub "fail loudly" philosophy even though this isn't a cloud stub.
- [x] `ACRRegistryProvider` (`app/cloud/registry.py`) — real implementation via the
      standard ACR OAuth2 token-exchange flow: `DefaultAzureCredential` (audience
      `https://containerregistry.azure.net`, confirmed against Microsoft's own
      `ContainerRegistryClient` default audience via Microsoft Learn, not guessed) →
      `POST <endpoint>/oauth2/exchange` for an ACR refresh token → used as the password
      half of a Docker Registry v2 basic-auth push via aiodocker (the same flow
      `az acr login`/`docker login` perform, without shelling out to `az`). Structurally
      correct; **not exercised live this session** — no Azure credentials/environment
      available — the same honest "real code, unverified live" flag Phase 3 already
      carries for untested gVisor. `AWSImageRegistryProvider`/`GCPImageRegistryProvider`
      stubs added alongside it (doc §9's own literal text: "ECR/Artifact Registry
      support coming soon"), selectable via `image_registry.provider` so a
      misconfiguration fails loudly at startup rather than silently.
- [x] `LocalImageStore` actually wired up (`app/cloud/registry.py`) — retags + pushes
      to the compose `registry:2` service via aiodocker; `Registry.resolve_component_image()`
      (new — `app/extensions/loader.py`) is the single shared resolver
      `template_render.py`/`sandbox_service.py` both now call instead of duplicating
      `source.type == "image"` inline checks, falling back to `Registry.built_images`
      for anything BuildManager produced. `DockerProvisioner` needed no code change to
      actually pull from the registry — aiodocker's `containers.run()` already
      pulls-if-missing, the same mechanism that made manual `docker build`s work
      before; live-verification below proves this pull path for real by removing the
      daemon's cached copy of the *pushed* tag first.
- [x] Any component published as a golden image via an automated pipeline — four new
      demo components, one per strategy, each built via `POST /v1/components/{name}/build`
      instead of a manual `docker build`: `components/tools/jq` (dockerfile),
      `components/tools/ripgrep` (compose), `components/tools/httpie` (pipeline, with a
      real cache-hit-skips-steps rebuild), `components/services/demo-echo` (helm — a
      rendered-manifest artifact, not an image; see "Known scope boundaries"). None of
      Phase 1–5's six existing components were touched or repointed — they stay on
      their original manual-build path on purpose, avoiding any risk to already
      live-verified behavior.

### Prerequisites this phase needed but the checklist didn't spell out

- `Registry.component_dirs`/`Registry.built_images` (`app/extensions/loader.py`) — the
  disk loader previously discarded a component's on-disk directory after validating
  it; BuildManager needs it to find the Dockerfile/compose file/chart. `built_images`
  is the write-through cache that makes a freshly-built component immediately runnable
  without a restart.
- The `builds` table (`app/persistence/models.py` + Alembic revision `d10dc67d3bce`) —
  doc §10.1 lists it as Phase-0 schema, but it was never actually added; confirmed
  absent before this phase (same "flagged, not silently assumed done" discovery as
  Phase 4/5's own prerequisites section). Migration verified to upgrade **and**
  downgrade cleanly against a throwaway SQLite DB (Phase 0's exact pattern — no live
  Postgres available to generate against here either).
- A minimal, real `ObjectStorageProvider` (`app/cloud/storage.py`) — `MinIOStorageProvider`
  (real, aioboto3-based S3 client against the compose MinIO service, running since
  Phase 1 but unused until now) and `AzureBlobStorageProvider` (real, same
  "unverified live, no Azure creds" flag as `ACRRegistryProvider`) plus AWS/GCP stubs.
  Pulled forward from its natural home (roadmap Phase 9) by explicit decision, because
  two Phase 6 strategies have an immediate, concrete need for it (pipeline caching,
  helm artifact storage) — not built speculatively ahead of a real caller.
  `SecretsProvider` stays untouched; nothing this phase needs it.
- `POST /v1/components/{name}/build` + `GET /v1/builds/{id}` (`app/api/v1/builds.py`)
  — not spelled out as a separate endpoint pair in doc §17's illustrative API surface
  (which only lists the trigger endpoint), but a multi-minute build absolutely needs a
  poll target, the same reasoning that gave `/v1/execute` its `?async=true` +
  `GET /v1/runs/{run_id}` pair (doc §5.1).
- `BuildError`/`BuildNotFoundError` (`app/core/errors.py`).

### Known scope boundaries (Phase 6)

- **No Kaniko/Kubernetes-Job build path for `aks-prod`.** `DockerfileBuildStrategy`
  only builds against the local Docker daemon — a real Kaniko-via-K8s-Job path (the
  control plane scheduling a Job with the Kaniko image, tailing its logs, waiting for
  completion) is a materially larger, separate piece of work that can't be
  live-verified in this environment anyway (no AKS, and the kind cluster from earlier
  phases was already torn down per Phase 4's "Known scope boundaries") — an explicit
  decision made with the user before implementation, not an oversight.
- **`ACRRegistryProvider`/`AzureBlobStorageProvider` are real implementations, never
  exercised against real Azure.** No Azure credentials/environment are available in
  this session — flagged the same way Phase 3 flags gVisor as "wired but untested."
- **`ComposeBuildStrategy` builds each declared service's image — it does not
  auto-translate a multi-service compose file into `SidecarSpec`s.** Phase 5 already
  covers real sidecar composition via hand-authored `SandboxTemplate`s; re-deriving
  that automatically from a compose file would be a separate, unrequested feature.
- **`HelmChartStrategy` renders and stores a manifest — it does not wire it into a
  running sandbox pod.** No existing doc section describes how a helm-rendered
  service composes into a `SidecarSpec` (Phase 5's sidecars are all
  `source.type: image`); a real, unaddressed gap, flagged rather than silently faked.
- **`PipelineBuildStrategy`'s declared `steps` are pre-build hooks, not the actual
  packaging step.** Doc's own example has a step shell out to `kaniko --destination
  $IMAGE` directly; since real Kaniko is out of scope and shelling to the `docker` CLI
  would add a prerequisite this codebase otherwise avoids entirely (every other Docker
  interaction goes through aiodocker, never the CLI), the strategy runs declared steps
  as pre-build hooks (with `$IMAGE`/`$COMPONENT_NAME`/`$COMPONENT_VERSION` in their
  environment) then packages the image itself via the same helper `dockerfile.py` uses.
- **`POST /v1/components` (registering a manifest via the API) doesn't accept build
  context files.** A dockerfile/compose/pipeline/helm-sourced component's Dockerfile/
  compose.yaml/chart/ must exist on disk under `components/` (doc §3.5's actual stated
  source of truth) — the same path every Phase 1–5 component was added through. The
  JSON-body `POST /v1/components` endpoint still accepts a manifest for any
  `source.type` structurally, but there's no mechanism to upload the accompanying
  build-context files through that same call; a real content-upload path for
  API-registered non-image components is an unaddressed gap, not silently pretended to
  work.
- **`quotas` table and `QuotaService`** remain entirely out of this phase's scope —
  they're Cross-cutting checklist items, not part of Phase 6's 8 deliverables.

### Bugs found and fixed during live verification (relayed hand-off, then direct)

Started as a relayed hand-off (the user ran `scripts/verify_phase6.py`, pasted output
back) and finished with direct Docker access once it turned out this session's
sandbox shares the user's own Docker daemon — same live-daemon-only discovery pattern
as every prior phase's bugs, just a mixed-mechanism session instead of purely relayed:

1. **`containers.run()` (create+start) can return before the container has actually
   settled into Docker's "running" state.** Invisible for every already-built,
   previously-run golden image from Phases 1–5 (the daemon already has every layer
   extracted and snapshotted from prior runs) — but confirmed live to be a real race
   the *very first* time a container is ever created from a freshly-built image:
   `jq`'s and `ripgrep`'s first-ever `POST /v1/execute` both failed with `put_files`
   hitting a genuine `[409] container ... is not running`, not a flaky one-off (same
   failure reproduced twice for `jq`, then again for `ripgrep`). **Fix:** added
   `DockerProvisioner._wait_container_running()` — a bounded poll (10s) on
   `container.show()`'s `State.Running`, called right after `container.run()` succeeds
   and before returning the handle; raises a clear `ProvisionerError` (including the
   container's actual state/exit code) if it never gets there, instead of letting a
   later, less-diagnostic 409 surface from an unrelated call site.
2. **`MinIOStorageProvider.get()` didn't handle a not-yet-existing bucket.** Only
   `put()` called `_ensure_bucket()`; the very first cache lookup a
   `PipelineBuildStrategy` (or `HelmChartStrategy`) ever makes — before anything has
   ever been written — hits a bucket that doesn't exist yet. Confirmed live: `httpie`'s
   first build failed with `An error occurred (NoSuchBucket) when calling the
   GetObject operation`, not the `KeyError` the cache-miss contract expects.
   **Fix:** `get()` now also calls `_ensure_bucket()` up front, matching `put()` —
   a missing bucket and a missing key are semantically the same "not found" case.

### Known environment limitation found — not a KubeSandbox bug

Running a sandbox (`POST /v1/execute`/`/v1/sandboxes`) against **any** freshly-built
Phase 6 component failed on the verification machine with the container exiting
immediately (`exec /usr/bin/sleep: operation not permitted`, exit code 255) — even
after fix #1 above (the container reliably reaches "exited", not "running", so the
wait times out with a clear diagnostic instead of hanging). Root-caused via
`journalctl -k`:

```
apparmor="DENIED" operation="exec" class="file" info="no new privs"
profile="snap.docker.dockerd" name="/usr/bin/sleep" ... target="docker-default"
```

This machine's Docker is **snap-installed** (`snap list`: `docker 29.3.1 ... canonical**`).
Snap's own AppArmor confinement profile wraps `dockerd`/`runc` themselves and — on
this host — refuses the AppArmor profile transition a container's own init process
needs when `no_new_privs` (`SecurityOpt: ["no-new-privileges"]`, doc §6 Layer 1 —
the same flag Kubernetes' `allowPrivilegeEscalation: false` maps to) is set, even for
a completely vanilla, unmodified `debian:12-slim sleep infinity` with **no other**
hardening flags applied and running as root. Confirmed this is unrelated to anything
in this repo's own Dockerfiles/images by reproducing it against that plain upstream
image directly, bypassing KubeSandbox entirely. `no-new-privileges` is applied
unconditionally to every sandbox container regardless of component — so this isn't
Phase-6-specific or new-component-specific either; it would identically block
`python`/`node`/`go`/`base` if any of Phase 1–5's images were ever run on this same
machine (none happened to be — this session's Docker daemon had never built or run
any of them before now). **Not fixed in code**: weakening `no-new-privileges` to work
around one host's broken Docker packaging would defeat the actual security property
that flag exists for everywhere else. The standard remediation is switching from
`snap install docker` to Docker's official `apt`-based Engine install, which doesn't
wrap `dockerd` in an extra confining AppArmor profile.

### Live verification (Phase 6)

Direct (not relayed, once Docker access turned out to be available in this session) —
against the real Docker daemon, the real `registry:2`/`minio` containers from
`docker compose up -d`, both fixes above applied:

- **`jq` (dockerfile strategy)**: `POST /v1/components/jq/build` → real `docker build`
  (apt-get install of `jq`, ~76s cold) → pushed to `localhost:5000/kubesandbox/jq:1.0`
  → `GET /v1/builds/{id}` polled to `succeeded` with the full build log captured in
  `log_excerpt`. Confirmed `Registry.built_images["jq@1.0"]` was populated
  immediately, no restart needed.
- **`ripgrep` (compose strategy)**: same shape — `ComposeBuildStrategy` parsed
  `docker-compose.yaml`, built/tagged the `ripgrep` service via the same
  `build_image_from_dockerfile` helper, pushed successfully.
- **`httpie` (pipeline strategy)**: first build ran its declared steps for real
  (~3m15s, cold `httpie` apt install) and packaged the image; a **second**, separate
  trigger (not the same in-flight build — the first had already finished) hit a real
  cache hit against the actual MinIO container: `log_excerpt` reads `"cache hit for
  'httpie-1.0' — skipping 2 step(s)"`, confirming the steps genuinely did not re-run
  — the whole second build completed in ~1s instead of ~3m15s.
- **`MinIOStorageProvider`**: round-tripped directly against the real `minio`
  container (`put`/`get`), including the missing-bucket cache-miss fix above.
- **Sandbox execution of a built component** (`POST /v1/execute` against `jq`/
  `ripgrep`) could not be confirmed end-to-end on this particular machine — blocked
  by the snap-Docker/AppArmor host issue above, unrelated to the build system itself.
- **`demo-echo` (helm strategy)** and **`ACRRegistryProvider`/`AzureBlobStorageProvider`**
  remain unverified live — `helm` isn't installed on this machine (documented optional
  prerequisite, README.md) and no Azure credentials are available, exactly as flagged
  going in.
- Two harmless orphaned debug containers (`jq-t1`/`jq-t2`, plain `debian:12-slim sleep
  infinity`, created manually while bisecting the AppArmor issue above) could not be
  removed — `docker kill`/`rm` both fail with `permission denied`, itself another
  symptom of the same snap/AppArmor confinement, not a KubeSandbox-related leak.

---

## Phase 7 — Pooling & persistence (6/6, implemented and unit-tested; live verification partial — same host limitation as Phase 6)

- [x] `PoolManager` service (`app/services/pool_manager.py`) — atomic claim via
      `SELECT ... FOR UPDATE SKIP LOCKED` against a new `pool_members` table
      (Postgres, not Redis — see "Design decisions" below), `release()` (recycle then
      re-list as claimable, falling back to `destroy()` on a `recycle()` failure so a
      broken container/pod never leaks into the pool), and `replenish_one()` (tops up
      one `(image_ref, weight_class)` key to a target count). Scoped to ad-hoc,
      sidecar-less, non-persistent, non-heavy-uncapped specs only
      (`PoolManager.is_poolable()`) — matching doc §4.3's own framing around bare
      "batch/workflow runs," never a SandboxTemplate composing DB sidecars (which need
      per-tenant credentials and `on_provision` hooks run fresh) or a persistent
      sandbox (tied to one specific workspace's durable volume/PVC).
- [x] Weight-class-based segregation (light/standard/heavy) — two different
      mechanisms per backend, both real: `app/services/weight_class_scheduler.py`'s
      `WeightClassScheduler` caps concurrent `heavy` sandboxes via an in-process
      `asyncio.Semaphore` sized from `pool.heavy_max_concurrent`, held for
      `execute()`'s whole acquire→run→release/destroy lifetime — the doc §7 "separate
      resource budget/queue in local" stand-in, correct only because `local` always
      runs exactly one control-plane replica. `KubernetesProvisioner` instead sets
      `nodeSelector`/`tolerations` on `heavy` pods from
      `provisioner.heavy_node_selector`/`heavy_tolerations`
      (`SandboxService._apply_heavy_segregation`) — the real, doc-described node-pool
      segregation mechanism for `aks-prod`.
- [x] Warm-pool claim wiring inside `acquire()` — `SandboxService._acquire_for_execute`
      tries `PoolManager.try_claim()` before `provisioner.acquire()`, for both
      `execute()` and `create_sandbox()` (doc §4.3: "a session may originate from a
      pool claim... but is immediately promoted to a dedicated, non-recycled pod" —
      `create_sandbox()`/`destroy_sandbox()` may claim from the pool for a fast start
      but never release back to it, matching that exact promotion language).
      `execute()` releases a clean, poolable run back to the pool instead of
      destroying it; a timed-out/truncated run, an exception, or a non-poolable spec
      still always destroys, unchanged from Phase 1–6 behavior. Pooling is opt-in per
      deployment (`pool.enabled`, false by default in `local.yaml`, true in
      `aks-prod.yaml`) — `pool_manager=None` (the `SandboxService` constructor
      default) reproduces every pre-Phase-7 test's exact expected behavior unchanged.
- [x] Persistent workspaces — Docker: a named volume (`kubesandbox-ws-<workspace_id>`)
      mounted at `/workspace` instead of tmpfs when `SandboxSpec.workspace_id` is set;
      ownership fixed via one root `exec` (`CapAdd: ["CHOWN"]` alongside the existing
      `CapDrop: ["ALL"]` — the same capability-restoration pattern Phase 5's DB
      sidecar bootstrap fix already established) since a fresh named volume's mount
      point is root-owned and a regular volume mount has no `uid=`/`gid=` option the
      way tmpfs does. Kubernetes: a persistent sandbox reuses one
      **long-lived, per-workspace namespace** (`<prefix>ws-<workspace_id>`, holding a
      PVC) across its own create/destroy cycles instead of a fresh per-sandbox-id
      namespace — required because a PVC is namespace-scoped and Kubernetes has no
      cross-namespace PVC mount; `destroy()` on a persistent sandbox
      (`SandboxHandle.persistent`, propagated from the `Sandbox.persistent` DB column
      via `_handle_from_row`) only deletes the Pod, never the namespace. A stale Pod
      left behind by a prior ungraceful teardown is detected and cleared
      (`_ensure_no_stale_pod`) before a fresh one is created. Wired into
      `SandboxService.create_sandbox(persistent=True)` via a new
      `WorkspaceService.get_or_create()` (lazy, one `Workspace` row per user) +
      `check_quota()` (soft — see "Known scope boundaries"). Guards against silently
      losing data: creating a persistent sandbox against an `archived`/`deleted`
      workspace raises rather than mounting a fresh, empty volume/PVC under the same
      name (no restore path exists — see below).
- [x] Workspace quota/retention enforcement — `WorkspaceService.sweep_retention()`
      implements doc §10.2's exact state machine (`active -> archived -> deleted`),
      driven by the reconciler: `active`, idle ≥ `idle_retention_days` (30) **or**
      age ≥ `max_lifetime_days` (365, "requires explicit renewal... else follows the
      same archive→delete path") → archive; `archived`, idle ≥
      `idle_retention_days + archive_grace_days` (90 total) → purge. Both thresholds
      measured from `last_access_at` (touched on every `create_sandbox(persistent=True)`,
      `run_in_sandbox()`, and `open_pty()` against a persistent sandbox), per doc's own
      "last session activity" wording, not whenever archival happened to run. A
      workspace whose own sandbox is still live is skipped, not archived out from
      under it (`_has_live_sandbox` check). New `Provisioner.archive_workspace()`
      (tars a persistent volume/PVC's contents via a short-lived throwaway
      container/pod — reusing the exact same exec-based plumbing as every other
      sandbox I/O path, never `get_archive`/`docker cp`, per Phase 1's established
      lesson), `Provisioner.delete_workspace_volume()` (removes the durable
      volume/namespace entirely), `Provisioner.measure_workspace_usage()` (`du -sm`
      against the same throwaway-mount pattern — refreshed every sweep for an idle
      `active` workspace, so `check_quota()` enforces against a real, current number
      instead of a permanent zero), and `Provisioner.restore_workspace()` (untars a
      cold-storage archive back onto a fresh volume/PVC, paired with
      `WorkspaceService.restore()` to explicitly bring an `archived` workspace back to
      `active`) round out the Protocol; `ObjectStorageProvider` gained a `delete()`
      method (MinIO/Azure real, AWS/GCP stubs) for the purge step.
- [x] `app/reconciler/loop.py` — a real `ReconcilerLoop` (constructs its own
      Provisioner/Registry/session-factory/ObjectStorageProvider via a new
      `app/core/bootstrap.py`, shared with `app/main.py`'s lifespan so the two never
      drift apart), runnable standalone via `uv run python -m app.reconciler.loop`
      (doc's own "a dedicated worker," not an in-API background task). Each
      fixed-interval tick (`reconciler.interval_seconds`, default 30s) runs four
      independent jobs: **TTL reaping** (destroys any non-terminated `Sandbox` past
      its `idle_ttl_seconds`/`max_ttl_seconds` — new columns on `sandboxes`, resolved
      once at create time from a SandboxTemplate's `spec.ttl` or new `TTLSettings`
      defaults for an ad-hoc request, and persisted since the reconciler runs in a
      separate process with nothing else to read them from); **pool replenishment**
      (tops up every poolable language component's warm pool to its configured
      `pool.{light,standard,heavy}_pool_size`); **workspace retention sweep**; and
      **orphan GC** (a new `Provisioner.list_sandbox_refs()` lists every live,
      sandbox-id-labeled native resource — a Docker container via
      `containers.list(filters=...)` + `.show()`, a Kubernetes namespace via
      `list_namespace(label_selector=...)` — and destroys any with no matching,
      non-terminated `Sandbox` row and older than `reconciler.orphan_grace_seconds`,
      so a crash between `acquire()` and its row committing, or a control-plane
      restart mid-`destroy()`, can't leak a resource forever). One bad tick logs and
      moves on rather than killing the loop.

### Prerequisites this phase needed but the checklist didn't spell out

- `app/core/bootstrap.py` — `_build_provisioner`/`_build_image_registry_provider`/
  `_build_object_storage_provider` were private functions inside `app/main.py`'s
  lifespan; the reconciler needed the exact same construction logic in a second,
  genuinely separate process, so they moved to a shared module both import, rather
  than risking the two silently drifting apart over time.
- `build_ad_hoc_spec()` (`app/services/sandbox_service.py`) — extracted from
  `SandboxService._build_spec` (unchanged behavior, now a thin delegator) so the
  reconciler's pool-replenishment job can build the identical `SandboxSpec` for a
  registry component without needing a full `SandboxService` instance.
- `app/provisioners/resources.py::parse_duration_to_seconds()` — doc's own
  `SandboxTemplate.spec.ttl` examples ("15m", "2h") had no parser anywhere; needed to
  turn those into the `idle_ttl_seconds`/`max_ttl_seconds` columns TTL reaping reads.
- `SandboxHandle.persistent` and `SandboxSpec.workspace_id`/`workspace_size_mb`/
  `node_selector`/`tolerations` (`app/domain/execution.py`) — none of these concepts
  existed anywhere in the execution-domain model before this phase.
- `NativeSandboxRef` (`app/domain/execution.py`) — orphan GC's return shape from
  `list_sandbox_refs()`; carries a Docker main container's sidecar ids too (sidecars
  share the same `sandbox-id` label, so `destroy()` needs `sidecar_refs` populated to
  avoid leaking them when reaping an orphaned main container).

### Design decisions

- **Pool claim ledger lives in Postgres (`pool_members`), not Redis**, despite doc
  §10.1's literal "pool claim locks" wording. `SELECT ... FOR UPDATE SKIP LOCKED`
  against the same table that's the actual source of truth for "which sandbox is
  this" gives an atomic, exactly-once claim with no separate cache to keep in sync or
  leak on a crash between "claimed in Redis" and "reflected in Postgres" — still
  satisfies "any replica can serve any session" (doc §2), since the claim is one
  atomically-committed row mutation visible to every replica through the same
  database. `pool_state` (the doc-described aggregate table) is kept too, but purely
  as a recomputed-from-`pool_members` observability counter, not a claim mechanism.
- **Heavy-class segregation is genuinely two different mechanisms, not one
  abstracted over both backends** — a `local`-only in-process semaphore (correct only
  under `local`'s exactly-one-replica guarantee) versus `aks-prod`'s real
  node-pool-and-taint scheduling. Unifying these behind one interface would have
  meant either faking node-pool semantics in Docker (meaningless — Docker has no
  nodes) or faking a distributed semaphore for Kubernetes (unnecessary — the
  scheduler already does this natively via taints/tolerations).

### Bugs found and fixed during live verification (direct — this session had real Docker access)

Unlike Phases 4–6, this session had direct access to the same Docker daemon as the
user (confirmed via `docker ps` showing the compose infra already running), so
verification was direct rather than relayed — but still hit the identical
snap-Docker/AppArmor `no-new-privileges` block Phase 6 first found and left
unresolved on this specific host (see "Known environment limitation" below,
unchanged from Phase 6 — not re-litigated here).

1. **No bugs found in the pool/persistence/reconciler code itself** during this
   pass — every genuinely testable-here piece (the Alembic migration, `pool_members`
   claim/release/replenish logic, `WorkspaceService.sweep_retention()`'s state
   machine, the reconciler's four jobs, Docker named-volume create/delete, and the
   mount+ownership-fix mechanism isolated from `no-new-privileges`) worked correctly
   on the first or second live attempt — see "Live verification" below for exactly
   what was run and how. This is different from every prior phase's live-verification
   section, which each found 1–5 real bugs; the difference here is that Phase 7's
   design leaned on already-proven patterns from earlier phases (the `CapAdd`
   capability-restoration fix from Phase 5's bug #1, the exec-not-archive-API lesson
   from Phase 1, the container-not-yet-running poll from Phase 6) rather than novel
   Docker/K8s interactions, and unit tests with mocked `aiodocker`/`kubernetes_asyncio`
   clients caught the logic bugs (an incorrect fresh-namespace stale-pod check; see
   below) before they ever reached a live daemon.
2. **A logic bug caught by unit tests, not live verification**: the first
   `KubernetesProvisioner.acquire()` implementation for persistent sandboxes always
   called `_ensure_no_stale_pod()`, even for a namespace it had *just* created this
   same call — impossible to have a stale pod in a namespace that didn't exist a
   moment ago, but the mocked `read_namespaced_pod` (returning "found" by default in
   the test fixture) made `_ensure_no_stale_pod` think one existed, delete it, then
   wait for a pod that was never coming, timing out. Fixed by having
   `_ensure_persistent_namespace()` report whether the namespace already existed
   (reused) vs. was freshly created, and only checking for a stale pod in the reused
   case. Exactly the kind of bug a mocked-client unit test suite (this phase added
   `test_docker_provisioner.py`, the first-ever dedicated Docker provisioner unit
   tests, mirroring the pattern `test_kubernetes_provisioner.py` already used) is
   designed to catch before it ever reaches a live daemon.

### Live verification (Phase 7)

Direct, against the real Docker daemon (`docker ps` confirmed the compose infra
already running: Postgres/Redis/MinIO/registry) and the real Postgres instance:

- **Alembic migration** (`7f07c054e570`) applied to real Postgres, confirmed via
  `psql \d sandboxes`/`\d pool_members` that every new column/table landed exactly as
  modeled, then downgraded and re-upgraded cleanly (Phase 0's own verification
  pattern) — new columns/table gone after downgrade, back after re-upgrade.
- **Docker named volume create/delete** — round-tripped directly against the real
  daemon via `aiodocker` (`volumes.create()`/`.show()`/`.delete()`), confirming the
  exact mechanism `delete_workspace_volume()` uses.
- **Persistent-workspace mount + ownership fix, isolated from the `no-new-privileges`
  block** — `docker run` with the exact same `--mount type=volume,...`,
  `--cap-drop ALL --cap-add CHOWN` flags this phase's code constructs (but without
  `--security-opt no-new-privileges`, to isolate the mount/ownership logic from the
  unrelated, already-diagnosed host issue): `docker exec --user 0:0 ... chown
  10001:10001 /workspace` succeeded, and a subsequent `docker exec --user 10001:10001
  ... sh -c 'echo hello > /workspace/test.txt && cat /workspace/test.txt'` read back
  `hello` — confirming the mount+ownership-fix mechanism `DockerProvisioner.acquire()`
  implements for persistent workspaces is correct in isolation.
- **A real `DockerProvisioner.acquire()` call with `workspace_id` set**, against the
  real daemon with the `base` golden image freshly built for this — reproduced
  *exactly* Phase 6's documented AppArmor failure signature (`ExitCode: 255`,
  matching `exec /usr/bin/sleep: operation not permitted`), confirming this phase's
  new code path hits the same well-understood, pre-existing host limitation and
  nothing new or different about it.
- **The reconciler's real entrypoint** (`ReconcilerLoop.create()` + one `.tick()`) run
  directly against the real Postgres + Docker daemon with pooling/persistence both
  off (matching `local.yaml`'s defaults) — completed cleanly with an all-zero result
  (`reaped=0, pool_replenished=0, workspaces_archived=0, workspaces_purged=0,
  orphans_reaped=0`), confirming Settings loading, Registry loading, the session
  factory, and `DockerProvisioner` construction all wire together correctly outside
  of pytest. Confirmed no test data landed in the real `sandboxes`/`workspaces`/
  `pool_members` tables afterward.
- Full unit suite: **237 passing** (up from Phase 6's 159 — 78 new tests this phase,
  across `test_pool_manager.py`, `test_weight_class_scheduler.py`,
  `test_sandbox_pooling.py`, `test_docker_provisioner.py` (new — first-ever dedicated
  Docker provisioner unit tests), `test_kubernetes_provisioner.py`'s persistent-
  namespace/archive/measure/restore/node-selector additions, `test_workspace_service.py`
  (incl. `restore()`), `test_workspace_retention.py` (incl. usage-measurement
  refresh), `test_reconciler.py`, and `test_sandboxes_api.py`'s persistent-sandbox-
  over-HTTP additions).

**One leaked debug container from manual verification, not removable on this
host**: a plain `docker run` used to isolate the mount+ownership-fix logic (see
above) left `manual-ws-test` (and its volume, `kubesandbox-ws-manual-test`) running —
`docker rm -f`/`kill` against it fails with `permission denied`, the *same*
snap/AppArmor confinement Phase 6 already documented for its own two leaked debug
containers ("another symptom of the same snap/AppArmor confinement, not a
KubeSandbox-related leak"). Not fixed — this is host packaging, not application code;
flagged here rather than silently left, matching Phase 6's own precedent for the
identical class of issue.

### Known environment limitation found — not a KubeSandbox bug (same as Phase 6, reconfirmed)

Re-confirmed at the start of this phase's session, unchanged from Phase 6: this
machine's Docker is snap-installed, and its own AppArmor confinement conflicts with
the `no-new-privileges` flag every sandbox container gets (doc §6 Layer 1) —
reproduced again directly against a vanilla `debian:12-slim sleep 1` with only
`--security-opt no-new-privileges` applied (`exec /usr/bin/sleep: operation not
permitted`). This blocks live-verifying *any* code path that actually starts a
hardened sandbox container end-to-end on this host — old or new, Phase 7-specific or
not. Not fixed in code, for the same reason Phase 6 didn't fix it: weakening
`no-new-privileges` to work around one host's broken Docker packaging would defeat
the actual security property it exists for everywhere else. Remediation is unchanged:
switch from `snap install docker` to the official `apt`-based Docker Engine install.

### Known scope boundaries (Phase 7)

- ~~Workspace quota is soft, and `used_mb` is never actually measured~~ — **closed**
  after this section was first written: `Provisioner.measure_workspace_usage()`
  (`du -sm` against the same throwaway container/pod pattern `archive_workspace`
  uses) is real on both backends, and `WorkspaceService.sweep_retention()` now
  refreshes `used_mb` for every `active` workspace with no currently-live sandbox on
  each tick, before evaluating retention — `check_quota()` is genuinely enforced
  against a live-ish number now, not a permanent zero. Still soft in the sense doc
  §5.4 always meant it to be (no xfs project quota / cgroup-level hard cap), and
  measurement is skipped for a workspace with a live sandbox attached (a Kubernetes
  PVC is `ReadWriteOnce` — a second, measurement-only pod could fail to schedule if
  the real sandbox pod landed on a different node; not worth that risk for an
  advisory number) — a live workspace's `used_mb` only refreshes once its sandbox
  goes idle enough for the next sweep to catch it.
- ~~No restore path for an archived workspace~~ — **closed**: `Provisioner.
  restore_workspace()` (untars onto a fresh volume/PVC, recreating a Kubernetes
  workspace's namespace shell first if `delete_workspace_volume()` already removed
  it) plus `WorkspaceService.restore()` (fetches the cold-storage tar, calls it, flips
  the workspace back to `active`) are both real and unit-tested. Deliberately NOT
  invoked automatically by `create_sandbox(persistent=True)` — that still raises
  against a non-active workspace rather than silently deciding a slow, explicit
  cold-storage fetch is what the caller wanted as a side effect of "just create the
  sandbox"; a caller that wants the data back calls `WorkspaceService.restore()`
  first. No HTTP endpoint exposes this yet (e.g. `POST /v1/workspaces/{id}/restore`)
  — the service-layer primitive exists and is real, wiring a route to it is a small,
  separate follow-up, not attempted here since nothing asked for it yet.
- **`heavy_max_concurrent`'s semaphore only caps concurrency within one process.** By
  design (see "Design decisions" above) — `local` always runs exactly one replica
  (doc §7), so this is never meant to coordinate across replicas the way Redis or
  Postgres-backed locking would; `aks-prod` doesn't use it at all, relying on real
  node-pool taints/tolerations instead.
- **`heavy_node_selector`/`heavy_tolerations` are real, wired, never exercised
  against a real heavy-workload node pool.** No AKS infrastructure (or even a
  multi-node kind cluster) exists in this environment — same honest "real code,
  unverified live" flag Phase 3 carries for gVisor and Phase 6 carries for
  `ACRRegistryProvider`.
- **Orphan GC only covers ephemeral, `sandbox-id`-labeled resources** — a persistent
  workspace's namespace (labeled `workspace-id`, not `sandbox-id`) is deliberately out
  of its scope; that namespace's lifecycle is retention's job, not orphan GC's. A
  persistent namespace whose `Workspace` row was hard-deleted out of band (bypassing
  `sweep_retention()`) would not currently be caught by anything — a real, if narrow,
  gap.
- **No single template/spec combines a persistent workspace with DB sidecars in this
  phase's test coverage.** Nothing in the code actually prevents it (a persistent
  ad-hoc `create_sandbox()` request could in principle compose sidecars too), but it
  was never exercised — flagged rather than silently assumed to work.
- **The Kubernetes persistent-workspace path (per-workspace namespace, PVC, node
  segregation) was verified only against mocked `kubernetes_asyncio` clients in unit
  tests, not a real cluster** — the kind cluster from Phase 4's own "Known scope
  boundaries" was already torn down before this phase started, and standing a new one
  up wasn't attempted this pass (the user confirmed no `kubectl`/`kind` was readily
  available this session either). Same honest flag as Phase 3's untested gVisor.
- **`PoolState`'s aggregate `idle_count` is a recomputed observability counter, not
  load-bearing for anything** — doc §14's `pool_hit_rate` metric would read it, but no
  `GET /metrics` endpoint exists yet (roadmap Phase 9), so nothing actually surfaces
  it today.

---

## Phase 8 — Billing (6/6, implemented, unit-tested — 284 total tests passing —
and live-verified against real Postgres/HTTP for the blocking half of the exit
criterion; the successful-run half stops at the same pre-existing, unresolved
host limitation Phases 6/7 already documented. Includes a same-session follow-up
that closed every non-blocking known scope boundary, plus a new credit-request
workflow — see the "Follow-up" section below the original 6 items.)

- [x] `BillingService` (`app/services/billing_service.py`) — resolves a tenant's
      `BillingAccount.mode` (creating one, defaulted to `billing.default_mode`, on
      first use — same lazy-`get_or_create` shape as `WorkspaceService.get_or_create`)
      and dispatches `authorize()`/`record_usage()`/`settle()` to the matching
      `CostingStrategy`. Config-only constructor, session passed per call (the
      `WorkspaceService` pattern, not `EntitlementService`'s session-in-constructor
      one) since the same instance is reused across both the admin endpoints and
      `SandboxService`'s call sites. Opt-in like `PoolManager`/`WorkspaceService`
      before it: `app/api/deps.py::_build_sandbox_service` only constructs one (and
      passes it to `SandboxService`) when `settings.billing.enabled` is true — false
      by default in both `local.yaml`/`aks-prod.yaml`, since a fresh credit-mode
      tenant's zero wallet balance would otherwise block every sandbox creation the
      instant this was silently turned on. Also provides `set_mode()` and
      `add_pricing_rule()`, the service-layer primitives behind the two admin
      endpoints below.
- [x] `CreditBillingStrategy` — `authorize()` prices a `UsageEstimate` against the
      latest applicable `pricing_rules` row per resource type and hard-blocks (doc
      §13: "before any resource is provisioned") when the estimated cost exceeds
      `CreditWallet.balance`. `record_usage()` prices the actual `UsageEvent`,
      deducts it from the wallet in real time, and writes a `CreditLedgerEntry` audit
      row (`delta`, `reason`, `balance_after`) per deduction. `settle()` still
      produces a draft `Invoice` for reporting parity with PAYG (doc: "both
      strategies write to usage_records so reporting is uniform"), even though a
      credit tenant has already paid via the wallet in real time — not a bill on top
      of that.
- [x] `PayAsYouGoBillingStrategy` — `authorize()` is advisory-only (doc §13.2): with
      no `spend_cap` set (the default), always authorized; with one set, blocks only
      when this calendar-month-to-date's `usage_records` cost plus the new estimate
      would exceed it. `record_usage()` writes `usage_records` only — PAYG tenants
      have no wallet. `settle()` is genuinely "the real work" here: sums
      `usage_records` over a `BillingPeriod` into a draft `Invoice`
      (`status="draft"`) via the same `_generate_invoice_draft` helper
      `CreditBillingStrategy.settle()` calls, so both modes share one code path for
      it. Actual payment collection remains a deliberate stub everywhere (doc §13) —
      nothing beyond a draft `Invoice` row is ever produced.
- [x] `PATCH /v1/admin/tenants/{id}/billing` (`app/api/v1/admin.py`) — `{mode,
      spend_cap?}` exactly per doc §17's own illustrative body; admin-only, creates the
      tenant's `BillingAccount` if it doesn't exist yet.
- [x] `POST /v1/admin/pricing-rules` — `{resource_type, unit_cost, currency?,
      effective_from?}`; admin-only; always appends a new versioned row rather than
      replacing any existing rule for the same `resource_type` (doc §10.1's own
      "multiple rules... coexist" framing) — `effective_from` lets an admin schedule a
      future rate change without disturbing the currently-active one.
- [x] Usage-event recording wired into `SandboxService.execute()` — `_authorize_billing()`
      (a shared helper also used by `create_sandbox()`, see below) runs before
      `acquire()`, pricing a `UsageEstimate` derived from the resolved spec's
      resource *limits* × the run's `wall_clock_seconds` cap (doc §13's own
      pre-authorization framing is a ceiling check, not a promise of the final bill).
      After the run completes, `usage_events_for_run()` turns the resolved spec's
      resource limits × the run's *actual* `duration_ms` into `cpu_second`/
      `memory_gb_second` (+ `db_hour` per composed DB sidecar) `UsageEvent`s, each
      tied back to the real, already-flushed `Sandbox`/`Run` row ids, and
      `BillingService.record_usage()` prices/persists them in the same transaction as
      the `Run` row and the sandbox's `state="terminated"` flip. A no-op end to end
      (zero extra queries beyond the existing flush) whenever `billing_service is
      None`, reproducing every pre-Phase-8 test's exact behavior unchanged.

### Prerequisites this phase needed but the checklist didn't spell out

- `app/domain/billing.py` — `UsageEstimate`/`UsageEvent`/`AuthResult`/`BillingPeriod`
  dataclasses + the `CostingStrategy` Protocol (doc §13's own shown signatures,
  extended with an explicit `session` keyword — every other service in this codebase
  threads a caller-supplied `AsyncSession` rather than owning one — and an `account`
  keyword so a strategy never has to re-query the `BillingAccount` row
  `BillingService` already resolved).
- `BillingAuthorizationError` (`app/core/errors.py`) — a `QuotaExceededError`
  subclass, not a new top-level error type: doc §11 itself groups "credit
  balance/spend cap" alongside quotas ("quotas (max concurrent sandboxes, cpu/mem,
  monthly minutes, credit balance/spend cap) enforced by QuotaService/BillingService
  before create"), and the subclass inherits the existing 429 mapping in
  `app/main.py` with zero new exception-handler registration.
- `BillingSettings.enabled` (`app/core/config.py`) — the doc's own settings model only
  had `default_mode`; an explicit opt-in flag was needed for the same reason
  `pool.enabled`/`workspace.persistence_enabled` needed one before it — see the
  `BillingService` bullet above for why defaulting this to true anywhere would have
  been an immediate regression.
- `estimate_usage_for_spec()`/`usage_events_for_run()`/`db_sidecar_count()`
  (`app/services/billing_service.py`) — turn a `SandboxSpec` + a duration into
  `UsageEstimate`/`UsageEvent`s; shared by both `SandboxService` call sites so
  pre-authorization and actual recording are computed identically.

### Known scope boundaries (Phase 8)

- ~~No admin endpoint funds a tenant's credit wallet~~ — **closed** in the same
  session, once asked whether this was a real constraint or just deferred scope: see
  "Follow-up" below for `BillingService.adjust_credit()` +
  `POST /v1/admin/tenants/{id}/credit`, and the credit/overusage *request* workflow
  built on top of it.
- ~~Only `execute()`'s usage is ever recorded — `create_sandbox()`/`run_in_sandbox()`
  are authorized but never billed for their actual consumption~~ — **closed**: see
  "Follow-up" below for `destroy_sandbox()` now billing a non-ephemeral sandbox's real
  `created_at -> terminated_at` span.
- ~~`storage_gb_day` (persistent workspaces) is never emitted~~ — **closed**: see
  "Follow-up" below for the reconciler's new `bill_workspace_storage()` job.
- **Usage is priced by configured resource *limits* × wall-clock duration, not real
  per-container cgroup measurement.** No metrics pipeline exists yet to measure
  actual CPU/memory consumption (that's Prometheus wiring, roadmap Phase 9) — the
  same "honest, not literal telemetry" flag Phase 5 already gave `maxDbSizeMB`. Still
  open — genuinely needs Phase 9's metrics pipeline, not just more service-layer code.
- **PAYG's "billing cycle" is the calendar month to date**, with no `Invoice`/cycle-
  boundary bookkeeping to anchor it more precisely — simple, and not something an
  admin can misconfigure, but not doc-specified either. Still open.
- **No pricing rule for a resource type prices it as free (cost 0), logged rather
  than raised.** Deliberate: an admin who hasn't configured pricing yet shouldn't
  have every sandbox creation start failing with an opaque error the moment
  `billing.enabled` flips true — but it does mean an unpriced resource type silently
  contributes nothing to any bill until priced. Still open, by design.

### Follow-up: closing the three scope boundaries above, plus a new credit-request workflow

Prompted by an explicit question — "was this a real constraint, or deferred scope?" —
for all three; none were a genuine blocker (unlike, say, the AppArmor host issue or
missing Azure credentials), so all three were closed in the same session, plus a new,
not-in-doc-§13 feature the user asked for on top: a self-service "request more
credit/overusage headroom" workflow.

- **Wallet top-up**: `BillingService.adjust_credit()` (`app/services/billing_service.py`)
  — the only way to fund/correct a `CreditWallet` balance short of a direct DB
  write, writing a `CreditLedgerEntry` per call (positive `delta` = top-up, negative =
  deduction/correction). Exposed as `POST /v1/admin/tenants/{id}/credit`
  (`{delta, reason?}`), admin-only.
- **Non-ephemeral usage billed for real, at `destroy_sandbox()` time**: a new
  `SandboxService._spec_resources_for_row()` re-derives a persisted sandbox's
  `ResourceSpec` the same way `_sidecar_components_for_row()` already re-derives its
  sidecar Components (ad-hoc: `build_ad_hoc_spec(component).resources`; templated:
  `render_template(...).sandbox_spec.resources`). `destroy_sandbox()` now bills
  `usage_events_for_run()` against `now - row.created_at` before flipping the row to
  `terminated`. This required refactoring `estimate_usage_for_spec()`/
  `usage_events_for_run()` to take a bare `ResourceSpec` instead of a full
  `SandboxSpec` (all either ever read) — `destroy_sandbox()` has no image/command/etc.
  to reconstruct for an already-persisted sandbox, only its resource limits.
  `destroy_sandbox()` also gained an optional `now: datetime | None` parameter so the
  reconciler's `reap_expired_sandboxes()` can pass its own tick timestamp through —
  every sandbox reaped in one tick bills against the same `now` instead of each
  hitting a slightly different real wall-clock read (a real bug caught by a unit
  test *before* this even needed live verification — see "Bugs found" below). This
  also closes TTL-reaped sandboxes for free: `reap_expired_sandboxes()` already
  routed through `destroy_sandbox()` since Phase 7, so no separate reconciler wiring
  was needed for that path.
- **`storage_gb_day` wired into the reconciler**: a new `bill_workspace_storage()`
  job (`app/reconciler/loop.py`, the module docstring's new job #4) prices every
  `active` `Workspace`'s current `used_mb` against the tick interval as GB-days —
  `(used_mb / 1024) * (interval_seconds / 86_400)` — once per tick, skipping
  never-measured (`used_mb == 0`) workspaces. `Workspace` has no `tenant_id` column of
  its own (only `user_id`); resolved via `User.tenant_id` rather than adding one,
  since nothing else needs it. Deliberately NOT gated on "no live sandbox" the way
  `sweep_retention()`'s own `used_mb` *refresh* is (that gate exists to avoid a risky
  extra measurement pod on a `ReadWriteOnce` PVC, doc §10.2/Phase 7) — storage cost
  accrues whether or not a sandbox is currently attached, so billing against
  whatever `used_mb` last read is correct either way. `run_tick()` now also
  constructs a `BillingService` from `settings.billing.*`, gated on
  `billing.enabled` exactly like every other opt-in reconciler job.
- **New: a credit/overusage request workflow** (not in doc §13's original design —
  added because a tenant that just hit `BillingAuthorizationError` had no path
  forward besides an admin acting out of band). A new `credit_requests` table
  (Alembic revision `4657775fb046`, autogenerated and verified to upgrade **and**
  downgrade cleanly against real Postgres — the first migration in this repo
  generated against a live Postgres instance rather than a throwaway SQLite DB, since
  one was finally available this session): `tenant_id`, `user_id`, `amount`,
  `reason`, `status` (`pending`/`approved`/`denied`), `review_note`, `reviewed_by`,
  `created_at`, `reviewed_at`. `BillingService.request_credit()` (validates
  `amount > 0`, else raises), `list_credit_requests()` (tenant/status filterable), and
  `review_credit_request()` — approving applies `amount` *before* marking the request
  approved (so a failure applying it never leaves a request incorrectly marked
  approved): `adjust_credit()` for a credit-mode tenant, or a `spend_cap` increase for
  a PAYG-mode tenant (PAYG has no wallet, so a cap bump is its closest analog to "more
  headroom"). New self-service router `app/api/v1/billing.py`:
  `POST/GET /v1/billing/credit-requests` (any authenticated principal, scoped to
  their own tenant — never sees another tenant's requests). New admin endpoints in
  `app/api/v1/admin.py`: `GET /v1/admin/credit-requests` (tenant/status filterable),
  `PATCH /v1/admin/credit-requests/{id}` (`{approve, note?}`).

#### Bugs found and fixed during this follow-up

1. **`adjust_credit(tenant_id, delta: float, ...)` crashed with `TypeError:
   unsupported operand type(s) for +: 'float' and 'decimal.Decimal'`** the moment a
   real caller (`review_credit_request()`, passing `request.amount`) handed it a
   value read back from a `Numeric` column — SQLAlchemy returns `Decimal`, not
   `float`, for those regardless of the ORM's declared Python type hint. Every other
   arithmetic site in `billing_service.py` already defensively wraps a
   Numeric-sourced value in `float(...)` before mixing it with a plain float; this
   one didn't. Fixed at both ends: `review_credit_request()` now passes
   `float(request.amount)`, and `adjust_credit()` itself now coerces `delta` to
   `float` unconditionally on entry — belt-and-suspenders, since this exact class of
   bug (a Numeric column's `Decimal` meeting a plain `float` in arithmetic) is easy to
   reintroduce at a new call site later. Caught immediately by
   `test_approving_a_credit_request_tops_up_a_credit_mode_wallet`, never reached live
   verification.
2. **`destroy_sandbox()` ignored the reconciler's own tick `now` and always read
   real wall-clock time**, so a TTL-reaped sandbox with a deliberately old
   `created_at` (e.g. a test backdating it to isolate a specific duration) got billed
   for the gap between its backdated `created_at` and the *real* current time, not
   the reconciler's simulated `now` — surfaced as an assertion off by ~210 days in
   `test_reap_expired_sandboxes_bills_real_lifetime_usage_when_billing_enabled`
   before this was fixed. Fixed by giving `destroy_sandbox()` an optional
   `now: datetime | None = None` parameter (defaulting to real time when omitted,
   unchanged for every other caller) and having `reap_expired_sandboxes()` pass its
   own `now` through — every sandbox reaped in the same tick now bills against one
   consistent timestamp instead of drifting across individual `datetime.now(UTC)`
   reads.

### Live verification of this follow-up

- **Migration** (`4657775fb046`) applied to real Postgres, confirmed the exact
  `credit_requests` schema via `psql \d credit_requests` (columns, FKs to
  `tenants`/`users`), then downgraded and re-upgraded cleanly (Phase 0's own
  verification pattern) — table gone after downgrade, back after re-upgrade.
- **The full credit-request workflow driven over real HTTP** end to end
  (`KUBESANDBOX_BILLING__ENABLED=true uv run uvicorn app.main:app`, real Postgres):
  `POST /v1/billing/credit-requests` (created a real `pending` row) ->
  `GET /v1/billing/credit-requests` (the requester saw their own request) ->
  `PATCH /v1/admin/credit-requests/{id}` as a non-admin (real `403`, confirming a
  requester can't self-approve) -> the same call as admin (`{"approve": true}`,
  returned `status: "approved"`) -> a zero-delta
  `POST /v1/admin/tenants/{id}/credit` call read back the wallet balance, confirming
  the approved amount had actually landed (100 -> +250 -> 1250, matching this same
  tenant's balance from Phase 8's own earlier live-verification pass plus this
  request's amount).
- Full unit suite: **284 passing** (up from Phase 8's 262 — 22 new tests this
  follow-up: 3 for `adjust_credit()`, 8 for the credit-request service methods, 3 for
  `destroy_sandbox()`'s destroy-time billing (incl. idempotency and a
  `billing_service=None` regression test), 1 for TTL-reap billing, 4 for
  `bill_workspace_storage()`/its `run_tick()` wiring, and 3 new HTTP-level tests
  across `test_api_v1_endpoints.py`).

### Known scope boundaries (this follow-up)

- **No notification when a credit request is filed or reviewed** — purely a queued
  row; an admin has to poll `GET /v1/admin/credit-requests?status=pending`. No
  email/Slack/webhook integration exists anywhere in this codebase to hang one off
  of.
- **No rate limit on filing requests** — a tenant can file arbitrarily many pending
  requests; cross-cutting rate limiting (doc's own roadmap item, "Cross-cutting"
  section below) doesn't exist yet for any endpoint, not just this one.
- **Approving a PAYG request raises `spend_cap` but never clears it back down** —
  there's no corresponding "reduce my cap" request type; an admin can still `PATCH
  .../billing` directly to lower it.

### Live verification (Phase 8)

Direct (this session has real Docker/Postgres access, confirmed via `docker ps`
showing the compose infra already running) — but the same snap-Docker/AppArmor
`no-new-privileges` host limitation Phases 6/7 documented and left unresolved is
still present on this machine (reconfirmed directly: a vanilla `debian:12-slim sleep
1` with only `--security-opt no-new-privileges` still fails with `exec /usr/bin/sleep:
operation not permitted`), and this session's Docker daemon doesn't have the
`python`/`node`/`go` golden images built (only `base`/`jq`/`ripgrep`/`httpie` survive
from Phases 6/7's own sessions) — so, as with Phase 6/7, live verification is split
into what's reachable without starting a hardened sandbox container, and what isn't:

- **`BillingService` exercised directly against real Postgres** (not just the
  SQLite-backed unit tests): `authorize()`/`record_usage()`/`add_pricing_rule()`/
  `set_mode()`/`settle()` all round-tripped correctly — a `CreditWallet` was really
  debited in real time, a `CreditLedgerEntry` really landed, and a PAYG `settle()`
  produced a real draft `Invoice` row with the correct summed `total_cost`. This is
  also what surfaced a real, live-only-catchable Postgres behavior: `usage_records`'
  `sandbox_id`/`run_id` foreign keys are genuinely enforced (SQLite, which the unit
  tests run against, doesn't enforce FKs by default) — a synthetic `sandbox_id` not
  backed by a real `Sandbox` row is rejected with a real `ForeignKeyViolationError`.
  Not a bug: `SandboxService.execute()` always flushes its `Sandbox`/`Run` rows
  *before* calling `record_usage()`, so the real wiring never hits this; it was
  purely an artifact of a throwaway verification script's fake id, fixed by inserting
  a real `Sandbox` row first — but worth recording as exactly the kind of constraint
  only a real Postgres session enforces, the same lesson Phase 3's live kind-cluster
  pass drew about label-value validation mocks can't catch.
- **`POST /v1/admin/pricing-rules` and `PATCH /v1/admin/tenants/{id}/billing` driven
  over real HTTP** (`KUBESANDBOX_BILLING__ENABLED=true uv run uvicorn app.main:app`,
  local-dev auth-disabled principal, real Postgres) — both returned the expected
  bodies and landed real rows.
- **The doc §13 exit criterion's blocking half, confirmed live end to end over real
  HTTP with zero container involvement**: with `billing.enabled=true`, two real
  pricing rules configured, and the local-dev tenant's wallet unfunded (balance 0),
  `POST /v1/execute` returned a real `429` — `{"detail":"insufficient credit:
  estimated cost 0.7500 exceeds balance 0.0000"}` — confirming the block happens
  before `acquire()` is ever called, exactly as doc §13 specifies. Funding that same
  tenant's wallet directly (`INSERT INTO credit_wallets ...`, the exact gap flagged
  above) and retrying made the `429` disappear entirely — the request then failed
  with a `502` for an unrelated, expected reason (`pull access denied for
  kubesandbox/python` — that image was never built on this session's daemon),
  proving authorization now passes cleanly and the request proceeds exactly as far
  as pre-Phase-8 code would.
- **The doc §13 exit criterion's successful-run half — actually completing a run and
  seeing `usage_records`/wallet deduction land via the real HTTP path — could not be
  confirmed end-to-end on this machine**, blocked by the same pre-existing AppArmor
  limitation (and missing `python` image) Phase 6/7 already documented, unrelated to
  this phase's own code. Covered instead by `tests/unit/test_sandbox_billing.py`
  (`FakeProvisioner`-backed) plus the direct-`BillingService`-against-real-Postgres
  pass above, which together exercise every piece of the same path independently.
- Full unit suite: **262 passing** (up from Phase 7's 237 — 25 new tests this phase,
  across `test_billing_service.py` (strategies, pricing lookup/precedence, settle) and
  `test_sandbox_billing.py` (execute()/create_sandbox() gating + usage recording,
  including a regression test proving `billing_service=None` reproduces pre-Phase-8
  behavior exactly), plus two new HTTP-level admin-endpoint tests in
  `test_api_v1_endpoints.py`.
- Leftover local-dev-only test artifacts from this pass (harmless, matching every
  prior phase's own precedent of not scrubbing local dev Postgres after live
  verification): a couple of extra `tenants` rows, two `pricing_rules` rows
  (`cpu_second`/`memory_gb_second`), and a funded `credit_wallets` row for the
  `local-dev` tenant.

---

## Phase 9 — Prod hardening (7/7 implemented and unit-tested — 480 tests passing —
with live verification blocked on this session's environment: no `helm` binary, no
reachable Postgres, no Azure. See "Live verification" below for exactly what was and
wasn't confirmed.)

Phase 9 grew mid-session by explicit request: after the original seven items were
under way, the user said UI development is going on the roadmap and asked that this
phase leave the backend ready for frontend integration, then to "add relevant task
items for UI integration related hardening" and "verify listing endpoints, crud, etc."
That second body of work is its own section below ("Phase 9b"), because it is
materially larger than the seven roadmap items and closes gaps that predate this phase.

- [x] gVisor/Kata prod node pool provisioning — `deploy/azure/provision-nodepools.sh`
      + `deploy/azure/README.md`. Idempotent `az aks nodepool add` for a
      `KataMshvVmIsolation` pool (Azure exposes gVisor/Kata through Pod Sandboxing, not
      a raw `runsc` install, and it constrains VM size to nested-virtualization-capable
      SKUs). Autoscale floor deliberately **1**, not 0: this pool hosts every sandbox,
      so scaling to zero makes the first request after an idle period wait minutes for
      a node — against a call doc §5.1 bounds at 60s. Still cluster/infra-level and
      still outside the app's code, exactly as the roadmap scoped it; what's new is
      that it's a committed, reviewable script instead of tribal knowledge.
- [x] Heavy-workload segregated node pool — same script. Labelled
      `kubesandbox.io/workload-class=heavy`, tainted
      `kubesandbox.io/heavy=true:NoSchedule`, autoscale floor **0** (heavy sandboxes are
      rare and the VMs expensive; the taint is what makes zero safe, since nothing else
      can land there to hold the pool up). `config/settings/aks-prod.yaml`'s
      `heavy_node_selector` REPLACE_ME is now the real label, and
      `test_the_nodepool_script_matches_the_prod_config` asserts the two agree — they
      are one decision written in two places, and if they diverge heavy sandboxes either
      stop being segregated or stop scheduling, neither of which fails loudly.
- [x] HPA/PDB manifests — inside the Helm chart (below), not standalone:
      `templates/hpa.yaml` (autoscaling/v2, CPU **and** memory targets — the control
      plane is I/O-bound, so a replica holding hundreds of idle attach streams shows
      little CPU and real memory; `scaleDown.stabilizationWindowSeconds: 600` because
      scaling down evicts live terminals, doc §2) and `templates/pdb.yaml`
      (`minAvailable`, API only). No PDB or HPA over the reconciler, deliberately — see
      the chart's own notes and `test_no_hpa_targets_the_reconciler`.
- [x] `app/cloud/` implementations — the checklist entry was stale: `storage.py` and
      `registry.py` were pulled forward into Phase 6 (two build strategies needed them).
      What Phase 9 actually added is the missing third concern plus the fail-fast
      machinery the doc promised and nothing had ever exercised:
      - `app/cloud/secrets.py` — `SecretsProvider` Protocol, `DotenvSecretsProvider`
        (real, `local`'s backend: process env first, then a repo-root `.env`, with the
        same `-`/`.` -> `_` upper-cased normalization Key Vault effectively forces, so a
        secret name resolves identically in both environments),
        `AzureKeyVaultSecretsProvider` (real, `azure-keyvault-secrets` +
        `DefaultAzureCredential`, azure imports deliberately inside `get()` so `local`
        never pays for them), and AWS/GCP stubs.
      - `app/cloud/base.py` — `ComingSoonProvider` marker base + 
        `assert_cloud_provider_usable()`. The existing AWS/GCP stubs were refactored onto
        it, which gives the startup check a type-level seam instead of string-matching an
        exception message.
      - `bootstrap.validate_cloud_providers()`, called first thing in both the API
        lifespan and the reconciler's `main()`. Doc §9 requires an unimplemented cloud
        selection to be "caught at startup/config-validation time, not mid-request";
        before this, the stubs had never been proven to raise at all, which is the only
        thing separating a stub from a silent no-op.
- [x] `sdk/` — a separately installable `kubesandbox-sdk` package (its own
      `pyproject.toml`, `requires-python >=3.11`, **httpx as the only required
      dependency**) so a workflow-builder never inherits fastapi/sqlalchemy/
      kubernetes-asyncio/azure-*/opentelemetry just to POST JSON. `KubeSandboxClient`
      (sync — doc §17's stated consumer, since a workflow step blocks) and
      `AsyncKubeSandboxClient`, both over shared request semantics in `_transport.py`;
      typed dataclass models; a full status-code -> exception mapping; and
      `attach.py` behind an optional `[attach]` extra for the PTY protocol. Admin
      endpoints and manifest publishing are deliberately **not** exposed — see the
      README's "what this SDK does not cover".
- [x] `deploy/helm/kubesandbox/` — control-plane chart: API Deployment, reconciler
      Deployment, Service, HPA, PDB, ConfigMap, ServiceAccount + least-privilege
      ClusterRole, Key Vault CSI `SecretProviderClass`, Ingress, ServiceMonitor, an
      Alembic `upgrade head` **pre-install/pre-upgrade hook Job** (a hook, not an init
      container: with N replicas an init container runs the migration N times
      concurrently and Alembic is not safe under that), `values-local.yaml` for kind,
      and a `NOTES.txt` that warns about every still-unconfigured setting — including
      the two that block a UI outright.
- [x] Prometheus `/metrics` + OpenTelemetry tracing — `app/core/metrics.py` (every doc
      §14 metric), `app/api/v1/metrics.py` (`GET /metrics`, registered only when
      `observability.metrics_enabled`), `app/core/tracing.py` (OTLP exporter,
      FastAPI/httpx/SQLAlchemy auto-instrumentation, plus manual spans at the
      provisioner boundary — the one hop nothing auto-instruments, and on the Docker
      backend the *only* view into provisioning). Instrumentation is live in
      `SandboxService`, `PoolManager`'s claim path, `BuildManager`, `BillingService`,
      and the WS gateway.

### Two deliberate deviations from doc §14's literal metric list

Both because the Prometheus data model wants them this way, and both documented in
`app/core/metrics.py`'s module docstring:

- **`pool_hit_rate`** is a pair of counters (`kubesandbox_pool_claims_total{result=
  "hit"|"miss"}`), not a pre-computed ratio gauge. A ratio computed in-process is wrong
  the moment the process restarts or a second replica exists; the rate is derived at
  query time. The claim counter is also only incremented when pooling is actually
  enabled — counting every acquire as a "miss" with `pool.enabled: false` would make the
  derived rate read 0% for a feature that simply isn't on.
- **`sandboxes_active`** is a per-replica, in-process gauge of sandboxes *this process*
  holds in flight, not the cluster-wide count of live sandbox rows. The authoritative
  count lives in Postgres and outlives any control-plane process; exporting it would
  need either a DB-querying collector on every scrape or a push from the reconciler (a
  separate process with no HTTP server of its own). It resets to 0 on a replica restart
  — correct for "in flight here", wrong for "live in the data plane" — and a sandbox
  created by a previous process and destroyed by this one drives it negative.
  Reconciler-reaped sandboxes are the concrete case where the two genuinely differ.

### Prerequisites this phase needed but the checklist didn't spell out

- `Provisioner.backend_name` (`app/provisioners/base.py` + both implementations +
  `FakeProvisioner`) — the same value each stamps onto `SandboxHandle.backend`, but
  readable *before* a handle exists, which the `provision_latency` failure path needs.
- `ObservabilitySettings` / `CorsSettings` (`app/core/config.py`), plus new validators:
  tracing refuses to enable without an OTLP endpoint, CORS refuses to enable with no
  origins or with `*` alongside credentials (browsers reject that combination outright),
  Key Vault refuses without a vault URL.
- `ConfigurationError` / `SecretNotFoundError` (`app/core/errors.py`).
- Six new dependencies: `azure-keyvault-secrets`, `prometheus-client`,
  `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc`, and the
  fastapi/httpx/sqlalchemy OTel instrumentations.

---

## Phase 9b — UI integration readiness (added mid-session by explicit request)

Not in the original roadmap. The user put UI development on the roadmap during this
session and asked that the backend be left ready for frontend integration before the
phase closed. An audit against "what does a browser UI actually need" turned up six
blockers, two of which are endpoints **doc §17/§5.1 specify and nothing had ever
implemented** — and which other documents in this repo already referenced as if they
existed.

### The two hard blockers a UI hits before anything else

- [x] **CORS** (`CorsSettings`, `app/main.py::_configure_cors`) — without it a
      cross-origin frontend cannot make a single call: the browser rejects the preflight
      before it reaches FastAPI, so nothing appears in server logs either. Off by
      default with an explicit allowlist (no wildcard default on a service that hands
      out sandbox sessions).
- [x] **OIDC/JWT session auth** (`app/services/auth_service.py`,
      `app/api/v1/auth.py`) — **this also closes the one open Phase 0 item**, doc §11's
      "OIDC (Azure AD) -> short-lived JWT session", which had no JWT-issuing or
      -validating code anywhere. The IdP's token is validated **once** against the
      issuer's JWKS at `POST /v1/auth/token` and exchanged for a KubeSandbox HS256
      session token carrying tenant/user/role — so `get_current_principal` becomes a
      local signature check with no database round trip, and the WS attach path (which
      must carry its credential in the query string, since browsers can't set headers on
      a handshake) carries a 1-hour token instead of a long-lived API key.
      `get_ws_principal` accepts `?access_token=` alongside `?api_key=`.

### Endpoints doc §17/§5.1 specify that had never been built

- [x] `GET /v1/runs/{run_id}` + `GET /v1/runs` (`app/api/v1/runs.py`) — doc §17 lists the
      first by name as the poll target, and Phase 6's own checklist notes already
      referenced "`/v1/execute`'s `?async=true` + `GET /v1/runs/{run_id}` pair" as
      though it existed. It didn't. The list endpoint isn't in doc §17 at all and is
      here because a "recent runs" view is the most obvious screen in a code-sandbox
      product and the data was already being persisted with nothing able to read it.
- [x] `POST /v1/execute?async=true` — doc §5.1's non-blocking variant, also never
      built. Returns `202 {run_id, status: "pending"}`; the spec is resolved *before*
      anything is scheduled, so a typo'd language still fails the triggering request
      with a real 404 rather than returning a cheerful 202 and failing invisibly later.
      Implemented by giving `execute()` an optional `existing_run_id` so the async path
      reuses one code path for pooling, sidecars, teardown, and billing rather than
      duplicating them.

### Read/list surface a UI cannot render without

- [x] `GET /v1/me` — identity, role, and **which optional subsystems are enabled here**.
      Every one of `persistent_workspaces`/`billing`/`pooling` is opt-in config, and a UI
      rendering a control for a disabled feature produces buttons that only ever 400.
- [x] `GET /v1/auth/config` — issuer/client-id/scopes so the SPA isn't hardcoding
      per-environment values. Unauthenticated by necessity (it's what you call before
      you have a credential) and returns nothing secret.
- [x] `GET /v1/sandboxes` — the list. Only create/get-by-id/delete existed, so a UI had
      no way to render "my sandboxes" and a leaked sandbox was invisible until its TTL
      reaped it. Reports last-known DB state rather than fanning out 50 provisioner
      calls; `GET /v1/sandboxes/{id}` remains the live-status endpoint.
- [x] `GET /v1/templates/{name}` — single-template detail with the resource/TTL/workspace
      shape. `GET /v1/components/{name}` existed; its template counterpart didn't, and a
      user choosing between templates is choosing CPU, memory, TTL, and persistence.
- [x] `GET /v1/builds` — build history list (only the by-id poll target existed).
- [x] `GET /v1/workspaces/me` — quota, usage, retention state. Nothing read a `Workspace`
      row back before this, so the whole persistent-workspace feature was invisible to a
      user until it silently stopped working. Returns three distinguishable states
      ("off here" / "none yet" / here it is) rather than collapsing two of them into a 404.
- [x] `GET /v1/billing/account` + `GET /v1/billing/usage` — doc §13 describes every
      mechanism that *consumes* a balance and nothing that reads one back, so a user hit
      with a 429 for "insufficient credit" had no way to see their own balance. Read-only;
      changing mode/cap/balance stays admin-only.
- [x] `GET /v1/admin/pricing-rules` — was POST-only, so an admin UI could not show what
      pricing was actually in force (rules are append-only, so "current" means "newest
      effective rule").
- [x] `GET /v1/admin/tenants` + `GET /v1/admin/users` + `PATCH /v1/admin/users/{id}/role`
      — every existing admin endpoint took a `tenant_id` the admin was assumed to already
      know. The role PATCH is also the only way to create an admin: `AuthService` never
      grants a role from a token claim, deliberately.

### Cross-cutting items closed on the way

- [x] **API-key issuance/revocation** (`app/api/v1/api_keys.py`) — was a cross-cutting
      open item: the table and hashed-lookup auth worked, but nothing could create or
      revoke a key except a direct DB insert. `POST/GET/DELETE /v1/api-keys`; 256 bits of
      entropy; plaintext returned exactly once and unrecoverable after. Also the bridge
      between doc §1's two consumers — a human signs into the UI with OIDC, then mints
      the key their workflow-builder uses, revocable without touching their identity.
- [x] **Pagination** (`app/api/pagination.py`) — a shared `Page` envelope
      (`items`/`total`/`limit`/`offset`) and `limit` capped at 200, applied to every
      collection that grows with *use*. Registry listings stay bare arrays: bounded by
      the git-committed registry, and changing their shape would break the SDK and
      existing tests for no gain. Ordering is newest-first with an `id` tiebreaker, so
      paging can't skip or repeat a row when two share a timestamp.
- [x] **A real `/readyz`** — it had been a Phase 0 stub returning `{"status": "ok"}`
      unconditionally, with its own docstring admitting real DB/Redis pings were still
      to come. Now probes Postgres and Redis concurrently (3s each, so two can't stack
      past the probe's own timeout) and answers 503 with a per-dependency breakdown.
      `/healthz` still deliberately checks nothing — conflating them is how a brief
      database blip restarts every replica at once.

### Schema changes (Alembic `9a1c4e77b210`)

Additive only, all nullable-or-defaulted, so it applies to a populated database and
rolls back cleanly:

- `api_keys`: `prefix` (something to display in a listing and match against a saved
  key), `created_by_user_id`, `last_used_at` (the one signal that makes "is this key
  still needed?" answerable before revoking; written on a coalesced best-effort basis).
- `runs`: `status` (server default `'completed'` — before this phase a `runs` row was
  only ever written *after* a run finished, so every historical row genuinely is one),
  `component_ref`, `error` (a control-plane failure that produced no result, distinct
  from the program's own stderr), `finished_at`.

**Hand-written rather than autogenerated**, unlike every migration before it: this
session had no reachable Postgres, and `alembic revision --autogenerate` against a
throwaway SQLite database hung partway through replaying the existing history. Every
operation is a plain `add_column`/`drop_column` pair — exactly what autogenerate would
have emitted — but it has **not** been applied to a real database yet. See "Live
verification" below.

### Documentation

- [x] `docs/UI_INTEGRATION.md` — the contract a frontend team works from: the two
      settings that block a UI outright, the login sequence, the PTY frame protocol and
      its three sharp edges, an endpoint map by screen, pagination and error semantics,
      and a "what the backend does *not* provide" section so the UI isn't designed
      around something that doesn't exist.
- [x] `sdk/README.md` — extended for the new surface.
- [x] `deploy/azure/README.md` — cluster prerequisites, in order, with the AAD app
      registrations honestly flagged as not automated.

---

## Bugs found and fixed during Phase 9

1. **`await self._json(...)["run_id"]` subscripted the coroutine, not its result** —
   `TypeError: 'coroutine' object is not subscriptable`, plus a "coroutine was never
   awaited" warning. Introduced by mechanically deriving the async client's new methods
   from the sync ones: the transformation is exact for one-liners, and this was the one
   line where `await` precedence made it wrong. Caught by
   `test_async_execute_then_wait_for_run`, and the reason
   `test_sync_and_async_clients_expose_the_same_surface` now exists.
2. **`last_used_at` bookkeeping could 500 a valid API key** — a
   `DateTime(timezone=True)` column comes back *aware* from Postgres but *naive* from
   SQLite, and `now - row.last_used_at` across the two raises `TypeError`. Because the
   comparison sat outside the surrounding `suppress(SQLAlchemyError)`, that would have
   turned a perfectly valid credential into a 500 on any backend returning naive values.
   Fixed with `deps._is_stale()`, which normalizes before comparing (every write to the
   column is `datetime.now(UTC)`, so assuming UTC is correct). Regression-tested both
   ways round.
3. **`FeatureFlags.interactive_attach` was a required field the server never supplied**
   — `GET /v1/me` raised a `ValidationError` on every call. A `Field(description=...)`
   with no `default` is required in pydantic; the description read like a constant and
   hid it.
4. **`start_async_run` accepted a run it could never execute** — with no
   `session_factory` wired, the 202 was already sent by the time the background task
   raised, producing `RuntimeError: Caught handled exception, but response already
   started` and a run stuck `pending` forever. Moved the guard to the *triggering*
   request, where there is still a caller to tell.
5. **`provision-nodepools.sh` was written into `deploy/helm/kubesandbox/templates/`** —
   a stale shell CWD from an earlier command. Inside a Helm chart's `templates/`, so
   `helm template` would have tried to render a bash script as a Go template. Caught by
   `test_the_nodepool_script_matches_the_prod_config` failing with `FileNotFoundError`,
   which is exactly the accident a path-asserting test is for.
6. **`_helpers.tpl` referenced `.Values.config.extraSettings`, absent from
   `values.yaml`** — guarded by `| default dict` so it *worked*, but an undeclared
   values path is invisible to anyone reading the chart. Declared it with an example.
   This is the failure mode `test_every_values_reference_exists_in_values_yaml` exists
   for: Go templates render a missing key as the empty string, so a typo produces a
   valid manifest with a silently wrong value.

---

## Live verification (Phase 9) — what was and wasn't confirmed

This session had **no reachable Postgres, no `helm` binary, and no Azure credentials**,
so the split is unusually wide. Being explicit rather than implying more than was done:

**Verified directly:**
- **`GET /metrics` end to end** over a real ASGI transport: 200, correct
  `text/plain; version=1.0.0` content type, and `kubesandbox_sandboxes_active` /
  `kubesandbox_runs_total` present with the expected label sets after being incremented.
- **Session-token security properties**, each confirmed to be rejected: a token signed
  with another secret, an `alg: none` token (algorithm confusion — the attack pinning
  `algorithms=["HS256"]` prevents), an expired token, a token missing `tid`/`role`/`exp`,
  and a token for another audience or issuer. Also that all failures return one
  indistinguishable message, so the error can't be used as an oracle.
- **The prod config guards**, in a real subprocess: `aks-prod` refuses the committed
  placeholder `jwt_secret`, refuses one shorter than 32 bytes (RFC 7518 §3.2), refuses
  `cors.enabled` with no origins, refuses `*` + credentials, refuses `tracing_enabled`
  with no endpoint, refuses `azure_keyvault` with no vault URL.
- **Doc §9's fail-fast contract**, which had never been exercised: every AWS/GCP stub
  raises rather than no-oping, and `validate_cloud_providers` turns each into a startup
  `ConfigurationError` naming the offending setting.
- **The whole `?async=true` -> poll -> bundled result cycle**, over the real routers.
- **Every v1 route requires a credential** — swept from the live OpenAPI spec rather
  than a hand-written list, so an endpoint added later can't quietly ship
  unauthenticated.
- **Tenant scoping on every new list/read endpoint**, against a two-tenant fixture (a
  single-tenant fixture cannot tell correct scoping from no scoping).
- **The Helm chart, statically**: every `.Values` path exists, every control block is
  closed, and the invariants its comments claim actually hold in the templates.
- Full unit suite: **480 passing** (up from Phase 8's 284 — 196 new tests across
  `test_sdk.py`, `test_cloud_secrets.py`, `test_auth_service.py`, `test_auth_deps.py`,
  `test_ui_api_surface.py`, `test_helm_chart.py`, and additions to `test_config.py`).

**NOT verified live, and why:**
- **Alembic `9a1c4e77b210` has never been applied to a real database.** No Postgres was
  reachable this session (`docker compose up` was requested from the user and the infra
  did not come up before the session ended). Every prior migration in this repo was
  verified to upgrade *and* downgrade cleanly; this one has not been. **It is the single
  highest-priority follow-up** — see below.
- **`helm lint` / `helm template` never ran** — no `helm` binary. The static test module
  covers the failure modes it could, and found two real problems doing so, but a
  successful render is not the same as a passing regex.
- **OIDC login against a real IdP.** No Azure AD tenant or app registration available.
  Everything on this side of the boundary is tested (claim mapping, provisioning, role
  rules, session tokens); the JWKS fetch and RS256 verification against a live issuer
  are not. Same standing flag `ACRRegistryProvider` and `AzureBlobStorageProvider`
  already carry.
- **`AzureKeyVaultSecretsProvider`** — real code, no credentials to run it against.
- **Node pool provisioning** — no AKS cluster. The script is idempotent and guarded but
  unrun.
- **OTLP tracing against a real collector** — none deployed. The config gate and the
  no-op-when-disabled path are tested; span export is not.
- **The `docker run --security-opt no-new-privileges` AppArmor limitation** from Phases
  6-8 was not re-checked this session, so whether sandbox execution works on this host
  is unknown.

### Required follow-up before this is deployable

```bash
docker compose up -d postgres redis minio registry
uv run alembic upgrade head          # apply 9a1c4e77b210
uv run alembic downgrade -1          # confirm it rolls back cleanly
uv run alembic upgrade head          # and re-applies
```

```bash
helm lint deploy/helm/kubesandbox
helm template ks deploy/helm/kubesandbox --set secrets.existingSecret=x | kubectl apply --dry-run=client -f -
helm template ks deploy/helm/kubesandbox -f deploy/helm/kubesandbox/values-local.yaml | kubectl apply --dry-run=client -f -
```

---

## Known scope boundaries (Phase 9)

- **No rate limiting.** Still open, still cross-cutting. Nothing throttles any endpoint,
  including `POST /v1/billing/credit-requests` and `POST /v1/api-keys` — a tenant can
  file unlimited requests or mint unlimited keys. Flagged in `docs/UI_INTEGRATION.md` so
  a UI doesn't assume server-side protection.
- **`QuotaService` is not implemented.** Doc §11's concurrent-sandbox caps, cpu/mem
  quotas, and monthly-minute quotas don't exist; only billing pre-authorization gates
  creation. A UI therefore cannot show a sandbox quota, because none is enforced.
- **No `AuditLog` writes.** The table has existed since Phase 0 and nothing writes to
  it, so there is no activity feed for a UI to render.
- **Async runs are not durable.** A control-plane restart mid-run leaves that row
  `running` forever; nothing resumes or reaps it. The same gap `BuildManager`'s
  background builds already have — the reconciler would be the natural owner of a sweep.
- **No full run logs.** `GET /v1/runs/{id}` returns the persisted 10 KB excerpt per
  stream. Doc §10.1 puts overflow in object storage; nothing writes or serves it.
- **Offset pagination**, so a row can shift between pages if the set changes mid-paging,
  and deep offsets get slow. Acceptable for tenant-scoped views of thousands of rows.
- **No push/subscribe for anything but PTY attach.** No SSE, no change feed — a UI
  dashboard must poll.
- **The first admin still requires a direct DB write** (or running locally with
  `auth.disabled`). `AuthService` deliberately never grants a role from a token claim,
  and `PATCH /v1/admin/users/{id}/role` requires an existing admin — so the bootstrap is
  a genuine chicken-and-egg. Documented rather than solved; an env-var-driven
  "bootstrap admin email" would be the obvious fix and is not implemented.
- **`sandboxes_active` and `pool_hit_rate` deviate from doc §14's literal shape** — see
  the reasoning above; both are deliberate.
- **Metrics are per-replica, single-worker.** The Helm chart runs one uvicorn worker per
  pod and scales via HPA replicas; a multi-worker pod would need
  `PROMETHEUS_MULTIPROC_DIR` and a shared-mmap registry.
- **AAD app registrations are not automated** (`deploy/azure/README.md` says so
  plainly) — they depend on choices this repo has no view into.

---

## Cross-cutting — not owned by a single phase (5/5)

All five closed. The three that remained after Phase 9b were done in a follow-up
hardening pass, together with the admin-bootstrap gap and the sandbox-hardening audit
below — 557 unit tests passing (up from 480).

- [x] `AuditLog` writes — **closed**. `app/services/audit_service.py` + a closed
      `subject.verb` action vocabulary, wired into sandbox create/destroy/run/attach,
      auth login and login-failure, API-key create/revoke, admin role/billing/credit/
      quota mutations, and every quota/billing/rate-limit **denial**. Readable via
      `GET /v1/admin/audit-logs` (admin-only, filterable by tenant/actor/action/target,
      paginated); pruned by a new reconciler job against `audit.retention_days`.

      Two design decisions worth recording. **Entries join the caller's transaction**
      (`session.add`, never its own commit) so an action that rolls back leaves no entry
      claiming it happened and an action that commits cannot commit without its entry —
      the usual "best-effort, never fail the request" pattern is wrong here, because an
      audit trail with silent holes is worse than none since it looks complete. Only
      events with no surrounding transaction (a login, a rejected credential) use a
      separate best-effort write. And **no code, stdin, stdout, or credentials are ever
      recorded** — identifiers, counts, and outcomes only; an audit log that accumulates
      user source becomes both a compliance liability and the most attractive table in
      the database. A test asserts submitted code never appears in an entry.

      On by default (`audit.enabled: true`), unlike every other subsystem added in these
      passes: doc §6 counts the audit log as a security *layer*, and a deployment that
      silently isn't recording actions is worse off than one that knows it isn't.
- [x] `QuotaService` — **closed**. `app/services/quota_service.py` +
      a new `quotas` table (doc §10.1 listed it; **it had never been created**).
      Concurrency, cpu-millicore, memory, and monthly-minute ceilings per tenant, checked
      before `acquire()` alongside billing pre-authorization, with
      `GET`/`PATCH /v1/admin/tenants/{id}/quota` and a self-service `GET /v1/me/quota`.

      Quotas and billing answer different questions, which is why both exist: billing asks
      whether a tenant can *afford* something (consumable, and a funded tenant continues
      indefinitely), quotas whether it should be *allowed* that much at once (a ceiling
      that binds regardless of funding). Since `billing.enabled` is false by default,
      before this **nothing at all** bounded a tenant's concurrency.
- [x] Rate limiting per API key/user — **closed**. `app/services/rate_limiter.py`:
      Redis-backed sliding window (a sorted set, so a caller can't send 2x the limit
      across a fixed-window boundary), three route classes with separate budgets
      (`execute`/`mutation`/`read` — one shared number would have to be set low enough
      for the expensive path), applied as per-route dependencies rather than global
      middleware so a route's cost is a visible property of the route rather than
      inferred from its path. Rejections carry `Retry-After` plus RFC 9331 `RateLimit-*`
      headers, and `RateLimit-Remaining` is set on *successful* responses too — a client
      that only learns its budget by being rejected can't avoid being rejected.

      Keyed on the **user** (or tenant for a service account), not per API key: any
      authenticated caller can mint more keys via `POST /v1/api-keys`, so a per-key budget
      would be trivially bypassable. **Fails open** on a Redis outage, deliberately —
      nothing here is a security boundary, and turning a Redis outage into a total API
      outage converts a degradation into an incident.
- [x] API-key issuance/management endpoints — **closed in Phase 9b**:
      `POST/GET/DELETE /v1/api-keys` (`app/api/v1/api_keys.py`), plus `prefix` /
      `created_by_user_id` / `last_used_at` columns so a listing is renderable and
      "is this key still in use?" is answerable before revoking.
- [x] `GET /metrics` — **closed in Phase 9**: `app/api/v1/metrics.py`, gated on
      `observability.metrics_enabled`, verified live over a real ASGI transport.

---

## Follow-up pass — admin bootstrap, cross-cutting, and hardening

Four asks, after Phase 9b: close the first-admin chicken-and-egg, implement the
cross-cutting items above, root-cause the host's Docker problem, and audit the codebase
against a standard 13-point sandbox-hardening list.

### Admin bootstrap (the "seeding" question)

Two mechanisms, because they cover different situations and neither alone is enough:

- [x] `auth.bootstrap_admin_emails` — an operator-configured allowlist, promoted to
      `admin` on OIDC login. The privilege it confers is narrower than it looks: listing
      an address creates nothing and grants nothing by itself, since someone must still
      authenticate against the real IdP *as that address*. An attacker needs both
      config-edit access and control of that mailbox — and anyone who can edit the
      deployment's config already owns the deployment.

      Evaluated on **every** login so adding an address later works without a redeploy
      dance, and deliberately **never demotes**: removing an address leaves an existing
      admin alone, because an admin promoted in-app through the proper endpoint must not
      be silently revoked by unrelated config drift. Case-insensitive, since an IdP may
      not preserve the casing a human typed into config.
- [x] `app/cli.py seed-admin` — for environments with **no IdP at all** (`local`, kind, a
      fresh prod database before the AAD app registration exists), where the allowlist
      does nothing because there is no login to promote on. Idempotent; refuses to
      promote an existing non-admin account unless `--promote-existing` is passed, since
      that account may belong to a different tenant. `list-admins` answers "is there an
      admin at all?", which is the question you have when a UI returns 403 and you don't
      know whether bootstrap ever happened.

      A CLI rather than an endpoint on purpose: running it requires shell access to a host
      that already holds the database credential, which is exactly the privilege level
      that *should* be needed to mint the first admin — and a much better boundary than an
      HTTP route behind a shared bootstrap token that then has to be rotated or disabled
      forever after.

`AuthService` still reads **no** role claim from a token (`roles`/`wids`/`groups` are all
ignored); a test asserts a token claiming every admin-shaped role still produces a plain
user. The allowlist is operator config, not token data, and that distinction is what makes
it safe.

### Sandbox hardening audit

Full item-by-item audit in **`docs/SECURITY_HARDENING.md`**. Eight of thirteen controls
were already in place; five needed work:

- [x] **Swap was unbounded** (Docker). The default `MemorySwap` is *twice* the memory
      limit, so a sandbox declaring 512MiB could touch 1GiB before the OOM killer
      intervened — with the extra half on host swap, unaccounted and orders of magnitude
      slower. Now pinned to `Memory`, Docker's documented "no swap at all". Sidecars too.
- [x] **tmpfs ignored the component's declared `ephemeralStorageMB`** — every writable
      path got a hardcoded `size=1g`. Now derived from the spec. Note the interaction that
      makes this safe rather than cosmetic: tmpfs pages are charged to the memory cgroup,
      so a tmpfs bomb OOM-kills the sandbox instead of filling the host disk — but only
      because swap is now disabled. The two fixes reinforce each other.
- [x] **Cloud metadata endpoint (IMDS) blocking** — the highest-value fix. It was already
      unreachable, but only *incidentally*: NetworkPolicy is additive and has no deny
      primitive, so the moment anyone adds the obvious rule ("allow 0.0.0.0/0 on 443 so
      pip works") IMDS reopens and nothing about that rule looks wrong. On a node with a
      managed identity, IMDS hands IAM credentials to anything that can reach it. Fixed
      with a second per-namespace policy allowing `0.0.0.0/0` **except** `169.254.0.0/16`,
      so the exclusion survives being unioned with a permissive rule; same pattern
      appended to both deploy overlays. The whole `/16`, since AWS also uses
      `169.254.170.2` for ECS task credentials.
- [x] **Explicit, configurable seccomp + AppArmor.** Docker previously relied on the
      daemon's implicit defaults — a daemon started with `--seccomp-profile=unconfined`
      would run an unfiltered sandbox with nothing in the logs to say so. Both are now
      named (`seccomp=builtin`, `apparmor=docker-default`) *and configurable*, which
      matters: naming an AppArmor profile on a host without AppArmor fails **every**
      container create, and RHEL-family hosts use SELinux instead — so
      `apparmor_profile: null` is the supported way to run there. Hardcoding it would have
      made the platform undeployable on those hosts.
- [x] **Kubernetes AppArmor** — `securityContext.appArmorProfile: RuntimeDefault` (a
      first-class field since 1.30) on both the sandbox pod and the workspace utility pod.
      `RuntimeDefault` rather than a named profile so it works on a gVisor/Kata node pool
      that may not carry a custom one.
- [ ] **Kubernetes per-pod PID limits** — a real gap between the two backends, and **not
      closeable from application code**: Kubernetes has no pod-level PID field, only the
      kubelet's `podPidsLimit`. `access.limits.processes` is honored on Docker and
      silently ignored on Kubernetes. Documented as a required node-pool setting in
      `deploy/azure/README.md` §7. A fork bomb on `aks-prod` is still bounded by the pod's
      memory limit, just by OOM rather than a clean ceiling.

### Bugs found and fixed during this pass

1. **The rate limiter systematically undercounted — worst under load.** The sorted-set
   member was `f"{now_ms}-{id(rule)}"`, which is the *same string* for every call in the
   same millisecond with the same rule object, so `zadd` **overwrote** instead of adding
   and the window never grew past one entry. Two concurrent requests collapsed into one
   slot; the limiter was least effective exactly when it was the only thing between a
   runaway client and the cluster. Fixed with a `uuid4` per request. Caught by
   `test_requests_are_allowed_up_to_the_limit_then_rejected`.
2. **The naive/aware datetime hazard, for the third time.** A
   `DateTime(timezone=True)` column comes back aware from Postgres and **naive** from
   SQLite, and mixing them raises `TypeError`. It had already bitten `deps._is_stale`
   (Phase 9), was latent behind a feature flag in `destroy_sandbox`'s billing arithmetic,
   and bit again in the new destroy-time audit entry. Third occurrence earned a shared
   home: `app/core/timeutil.py`'s `ensure_utc`/`elapsed_seconds`, now used by all three,
   with `elapsed_seconds` clamping negatives (a backdated row or an NTP step otherwise
   produces negative billed usage). The asymmetry is what makes this class of bug nasty —
   it is invisible in a SQLite-backed suite whenever the path only runs against Postgres,
   and invisible in production whenever it only runs in tests.
3. **Run audit entries were attributed to `system` instead of the user.** `_audit_run`
   passed no principal, so `actor_for(None)` returned the system actor for every user's
   own run — making the audit trail's single most important column wrong for its single
   most common entry. `SandboxService` threads ids rather than a `Principal`, so
   `actor_from_ids` was added and the actor threaded through `_persist_run_result`.
4. **`app.state`-backed dependencies 500'd without a lifespan**, breaking 38 tests. Fixed
   by giving each a considered fallback rather than a blanket one: audit falls back to a
   *functional* service (silently dropping audit writes because a lookup missed would
   defeat the point), rate limiting to a *disabled* one (it needs the Redis client the
   lifespan built, and "allow" matches its documented fail-open policy).

### Live verification (this pass)

Same environment constraints as Phase 9, and now with a root cause for them:

- **The host's Docker problem is a wedged root filesystem, not a Docker or AppArmor
  issue.** `jbd2/sda1-8` — the ext4 journal thread for `/` — is stuck in uninterruptible
  (`D`) sleep, along with **13 `umount` processes** for leftover **kind-cluster**
  containerd mounts (`/run/containerd/.../k8s.io/...`) and ~19 writeback kworkers. Load
  average **51 with zero containers running**. Every disk write queues behind the journal,
  which is why `docker run` hangs, `alembic` hung, and even `cat` on a cold file timed out
  earlier in these sessions. `D`-state processes ignore `SIGKILL`, so there is no
  userspace fix — it needs a reboot, and the leftover kind state should be cleared
  afterwards so it can't recur. Also worth correcting the record: the host is **Ubuntu
  Core 24** (all-snap by design), so Phase 6-8's "snap-packaged Docker + AppArmor"
  diagnosis was directionally right about confinement, but today's symptom is a hang
  rather than a permission error and has a different cause.
- **Unit suite: 557 passing** (up from 480 — 77 new tests across `test_hardening.py` and
  `test_hardening_api.py`, plus an updated `test_kubernetes_provisioner.py` for the second
  NetworkPolicy).
- **Still unverified live, unchanged from Phase 9**: both Alembic migrations
  (`9a1c4e77b210`, and the new `c3f81a92e740` for `quotas`) have never been applied to a
  real database; `helm lint`/`helm template` has never run; OIDC against a real IdP, Key
  Vault, node pools, gVisor, and OTLP export are all untested. The rate limiter has never
  run against real Redis — the sliding-window logic is covered by a fake implementing the
  sorted-set subset it uses, which is what caught the `zadd` bug, but Redis's own
  behaviour under concurrency is not exercised.

### Known scope boundaries (this pass)

- **Quota accounting uses per-weight-class budgets**, not each live sandbox's resolved
  cpu/memory limits. `sandboxes` doesn't persist resolved resources, and re-resolving each
  one's component/template on every quota check would mean registry lookups inside a
  request that is only trying to count. The budgets are deliberately *conservative* (at or
  above what a typical component in that class declares) so enforcement errs toward
  refusing. The exact fix is persisting resolved cpu/memory on the row — a schema change
  this pass didn't take.
- **Quota enforcement is check-then-act, not a reservation.** Between the count and the
  insert, concurrent requests can push a tenant one sandbox over its cap; over-admission
  is bounded by the number of simultaneous in-flight creates. A hard guarantee needs
  either `SELECT ... FOR UPDATE` on the quota row (serializing every create for a tenant)
  or a reservation table with a reaper for crashed reservations. Not worth it for a limit
  whose purpose is stopping a tenant taking hundreds of sandboxes, not exactly N.
- **Monthly-minute quota caps compute, not occupancy.** It sums `runs.duration_ms`, the
  only durable record of how long compute actually ran; a non-ephemeral sandbox's idle
  *lifetime* isn't included, because it has no `runs` row and double-counting a warm
  sandbox's runs plus its wall-clock lifetime would overstate usage badly. Concurrency
  caps occupancy instead.
- **No egress proxy or package mirror is shipped** — a deployment that needs `pip install`
  must provide one, and must add the rule carefully (see the IMDS note above for why).
- **No log shipping is configured**, which matters for the specific threat item 13
  addresses: anyone who can reach the database can edit `audit_logs`, so the
  tamper-resistant record has to be the shipped copy. The reconciler's pruning assumes it
  exists; if it doesn't, lengthen `audit.retention_days` or set it to `null`.
- **Audit coverage is not yet total.** Component/template publishing, build triggering,
  entitlement and publish-grant changes, and workspace file reads/writes have action
  constants defined but no call sites wired. The high-value paths (runs with exit codes,
  lifecycle, auth, keys, role changes, denials) are covered.
- **`SANDBOX_ATTACH_REJECTED` is defined but unwired** — the 409 path is in the WS gateway,
  which raises before the audit service is reachable in the dependency graph.
