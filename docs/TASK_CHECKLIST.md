# KubeSandbox — Exhaustive Task Checklist

Every task implied by `docs/ARCHITECTURE_AND_PLAN.md`, broken out phase by phase (§20),
with an honest per-item completion status as of 2026-07-18. This is a granular expansion
of the roadmap table, not a re-statement of it — each phase's one-line "deliverable"
below is exploded into the actual concrete pieces of work it implies across the rest of
the doc (§3–§19).

**Summary: 57 / 103 items complete — Phase 0 (20/21), Phase 1 (21/21, fully
live-verified), Phase 2 (10/10, fully live-verified), and Phase 3 (6/6, fully
live-verified against a real kind cluster). Phases 4–9 and the cross-cutting section
are 0% started.**
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
