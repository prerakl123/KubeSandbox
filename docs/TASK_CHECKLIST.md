# KubeSandbox — Exhaustive Task Checklist

Every task implied by `docs/ARCHITECTURE_AND_PLAN.md`, broken out phase by phase (§20),
with an honest per-item completion status as of 2026-07-18. This is a granular expansion
of the roadmap table, not a re-statement of it — each phase's one-line "deliverable"
below is exploded into the actual concrete pieces of work it implies across the rest of
the doc (§3–§19).

**Summary: 70 / 103 items complete — Phase 0 (20/21), Phase 1 (21/21, fully
live-verified), Phase 2 (10/10, fully live-verified), Phase 3 (6/6, fully
live-verified against a real kind cluster), Phase 4 (7/7, implemented,
unit-tested — 100 unit tests passing — and live-verified against a real Docker
daemon via a relayed hand-off loop, since this session has no direct Docker/kind
access), and Phase 5 (6/6, implemented, unit-tested — 137 unit tests passing — and
fully live-verified against all three real database engines via the same relayed
hand-off pattern as Phase 4: DSN injection, each scoped non-superuser role/ACL user,
and a rejected privilege-escalation attempt confirmed against live
`postgres:16-alpine`, `mysql:8.4`, and `redis:7-alpine` sidecars; four bugs found and
fixed along the way, see Phase 5's "Bugs found and fixed during live verification").
Phases 6–9 and the cross-cutting section remain 0% started.**
Phase 1 is no longer just "built" — it has been driven end-to-end against real Docker,
real Postgres, and the actual golden image, via `POST /v1/execute` returning correct
`stdout`/`stderr`/`exit_code`/`variables` with no leaked containers afterward. See
"Bugs found and fixed during live verification" at the end of this section — none of
these were catchable by the unit tests alone, precisely because they needed a real
Docker daemon to surface.

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

---

## Phase 0 — Foundations (20/21)

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
