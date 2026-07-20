# KubeSandbox

Sandbox-provisioning control plane. See `docs/ARCHITECTURE_AND_PLAN.md` for the full
design, and `docs/TASK_CHECKLIST.md` for an honest per-item completion status. This
covers local setup through the Phase 0–6 slice: config, DB, the `python`/`node`/`go`/
`bash`/`git`/`base` components, `DockerProvisioner` **and** `KubernetesProvisioner`,
`POST /v1/execute` (ad-hoc language or SandboxTemplate composition), the non-ephemeral
sandbox lifecycle (`POST /v1/sandboxes`, batch runs, file upload/download/tree, and
interactive PTY attach over WebSocket), the component/template/entitlement registry
APIs, the Kustomize sandbox-primitive manifests (NetworkPolicy/RBAC/ResourceQuota/
LimitRange/RuntimeClass), database sidecars (`postgresql`/`mysql`/`redis`
composed into a sandbox as a real second container, with a non-superuser role/ACL user
provisioned by a `ComponentHook` — see "Try a database sidecar" below; all three are
live-verified end to end against real containers, see `docs/TASK_CHECKLIST.md`'s
Phase 5 section), and the build system (`BuildManager` — dockerfile/compose/pipeline/
helm `BuildStrategy` implementations, `LocalImageStore`/`ACRRegistryProvider`,
`MinIOStorageProvider`/`AzureBlobStorageProvider` — see "Try the build system (Phase 6)"
below).

Full interactive API docs (every endpoint annotated — summaries, parameter
descriptions, grouped by tag) are served at `http://localhost:8000/docs` once the app
is running.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (manages the venv/deps — always run Python via
  `uv run ...`, not a bare `python3`, so the right interpreter/deps are used)
- Docker, with your user in the `docker` group (`sudo usermod -aG docker $USER`, then
  log out/in or `newgrp docker`/`sg docker -c '...'`) — `docker ps` should work without
  `sudo`
- Optional, only needed to exercise `KubernetesProvisioner` locally: `kubectl` and
  [`kind`](https://kind.sigs.k8s.io/) — see "Try the Kubernetes provisioner (Phase 3)"
  below. Both are plain binaries; no root required to install them (e.g. into
  `~/.local/bin`).
- Optional, only needed to build the `demo-echo` component: [`helm`](https://helm.sh/)
  — see "Try the build system (Phase 6)" below. `HelmChartStrategy` fails loudly with
  a clear error if it's missing rather than silently skipping.

## One-time setup

```bash
# 1. Install dependencies into .venv
uv sync

# 2. Start supporting infra (Postgres, Redis, MinIO, local image registry)
docker compose up -d

# 3. Build each `source.type: image` component's golden image manually — these six
#    (python/node/go/base + the three DB sidecars, which are pulled as-is) predate
#    BuildManager and stay on this one-time-manual-build path on purpose, matching
#    docs §8.1's local fallback; nothing about how they run changes in Phase 6. The
#    tags must match each component.yaml's spec.source.image exactly. All four below
#    include `dtach` (Phase 4 interactive attach reattach) — if you built these before
#    Phase 4, rebuild them, there's no other way to pick up the Dockerfile change.
#    The four NEW Phase 6 demo components (jq/ripgrep/httpie/demo-echo) are built by
#    BuildManager instead — see "Try the build system (Phase 6)" below, no manual
#    `docker build` needed for those.
docker build -t kubesandbox/python:3.12.4-slim components/languages/python
docker build -t kubesandbox/node:20.15.0-slim components/languages/node
docker build -t kubesandbox/go:1.22.5-slim components/languages/go
# base/bash/git share one image (a real, live-testable example of Phase 2's
# SandboxTemplate composition — see templates/base-dev-lab.yaml)
docker build -t kubesandbox/base:1.0 components/base

# 4. Apply DB migrations
uv run alembic upgrade head
```

## Run the app

```bash
uv run uvicorn app.main:app --reload
```

`APP_ENV` defaults to `local` (see `config/settings/local.yaml`), which points at the
`docker compose` services above on `localhost` and runs with `auth.disabled: true` for
convenience (guarded in `app/core/config.py` to never apply outside `local`).

## Try it

```bash
curl -s http://localhost:8000/v1/execute \
  -H 'Content-Type: application/json' \
  -d '{
        "language": "python",
        "code": "x = 21\nresult = x * 2\nprint(\"hello from the sandbox\")\nname = input()\nprint(\"got:\", name)",
        "stdin": "world"
      }' | python3 -m json.tool
```

Expected shape (doc §5.1) — one bundled result, no live streaming:

```json
{
  "run_id": "...",
  "exit_code": 0,
  "stdout": "hello from the sandbox\ngot: world\n",
  "stderr": "",
  "duration_ms": 123,
  "truncated": false,
  "timed_out": false,
  "variables": { "x": 21, "result": 42, "name": "world" }
}
```

### Try a SandboxTemplate (Phase 2 composition)

`templates/base-dev-lab.yaml` composes `base` + `bash` + `git` — a genuine
multi-component template, runnable today because all three share one pre-baked image.
Merging *separate* golden images into one running container is a distinct problem
BuildManager (Phase 6) doesn't solve either — it makes each individual component's
image real and pullable, but `render_template()` still requires every `mainTool`
component in a template to resolve to the exact same image (see
`docs/TASK_CHECKLIST.md`'s Phase 2 "Known scope boundaries"):

```bash
curl -s http://localhost:8000/v1/execute \
  -H 'Content-Type: application/json' \
  -d '{
        "template": "base-dev-lab@1.0",
        "language": "bash",
        "code": "git --version\necho hello from the composed sandbox"
      }' | python3 -m json.tool
```

### Try the sandbox lifecycle + interactive PTY attach (Phase 4)

Unlike `/v1/execute`'s ephemeral acquire→run→destroy, `POST /v1/sandboxes` creates a
sandbox that sticks around until you explicitly destroy it — which is what
interactive attach and repeated batch runs both need:

```bash
# 1. Create a sandbox (note the id in the response)
curl -s http://localhost:8000/v1/sandboxes \
  -H 'Content-Type: application/json' \
  -d '{"language": "python"}' | python3 -m json.tool

SANDBOX_ID=<paste the "id" from above>

# 2. Check its live status
curl -s http://localhost:8000/v1/sandboxes/$SANDBOX_ID | python3 -m json.tool

# 3. Run a batch command against it (doesn't destroy it, unlike /v1/execute)
curl -s http://localhost:8000/v1/sandboxes/$SANDBOX_ID/runs \
  -H 'Content-Type: application/json' \
  -d '{"code": "print(\"still here\")"}' | python3 -m json.tool

# 4. Upload/download/list workspace files
curl -s -X PUT "http://localhost:8000/v1/sandboxes/$SANDBOX_ID/files?path=notes.txt" \
  --data-binary "hello workspace"
curl -s "http://localhost:8000/v1/sandboxes/$SANDBOX_ID/files?path=notes.txt"
curl -s "http://localhost:8000/v1/sandboxes/$SANDBOX_ID/tree" | python3 -m json.tool

# 5. Attach an interactive terminal (needs the `websockets` dev dependency, already
#    in pyproject.toml's dev group)
uv run python scripts/ws_attach_demo.py $SANDBOX_ID
```

In the attach session, try `pwd`, `export FOO=bar`, `cd /tmp` — then Ctrl-D to
disconnect the demo client and re-run the same `ws_attach_demo.py` command. The
reattached session should still be in `/tmp` with `$FOO` still set: attach runs
`dtach -A ... /bin/bash` inside the sandbox, so a reattach resumes the *same* shell
rather than starting a fresh one (doc §5.2's "reattach as the same viewer") — not
tmux, which turned out not to work in these containers at all (see
`docs/TASK_CHECKLIST.md`'s Phase 4 "bug found and fixed during live verification").

While one client is attached, a second `ws_attach_demo.py` run against the same
`SANDBOX_ID` (from another terminal, before the first disconnects) should be rejected
outright — single-viewer enforcement, doc §5.2's "no collaboration in v1".

```bash
# 6. Tear it down when done
curl -s -X DELETE http://localhost:8000/v1/sandboxes/$SANDBOX_ID -w '%{http_code}\n'
```

### Try a database sidecar (Phase 5)

Three single-DB companion templates — `templates/python-{postgres,mysql,redis}-lab.yaml`
— each compose `python@3.12.4` (main) with one DB component as a real second
container, reachable from main over localhost only. No local `docker build` needed
for any of them: `postgres:16-alpine`/`mysql:8.4`/`redis:7-alpine` are pulled as-is
from Docker Hub, unlike the `kubesandbox/*` golden images. All three are
live-verified end to end against real containers (see `docs/TASK_CHECKLIST.md`'s
Phase 5 "Live verification" section).

`psql`/`mysql`/`redis-cli` live in each sidecar image, not main's (main's own DB
access would normally go through a client library, e.g. `psycopg2`, not shelling out
to a CLI) — so steps 3 below exec directly into the sidecar container rather than
through `/v1/sandboxes/.../runs`.

**Postgres:**

```bash
curl -s http://localhost:8000/v1/sandboxes \
  -H 'Content-Type: application/json' \
  -d '{"language": "python", "template": "python-postgres-lab@1.0"}' | python3 -m json.tool
SANDBOX_ID=<paste the "id" from above>

# main got a scoped DATABASE_URL it never saw the admin password for
# (components/databases/postgresql/hooks.py created this role, not main's image)
DSN=$(curl -s http://localhost:8000/v1/sandboxes/$SANDBOX_ID/runs \
  -H 'Content-Type: application/json' \
  -d '{"code": "import os\nprint(os.environ[\"DATABASE_URL\"])"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["stdout"].strip())')

docker exec "kubesandbox-${SANDBOX_ID}-postgresql" psql "$DSN" -c "select current_user, current_database();"
docker exec "kubesandbox-${SANDBOX_ID}-postgresql" psql "$DSN" -c "CREATE ROLE escalated SUPERUSER;"

curl -s -X DELETE http://localhost:8000/v1/sandboxes/$SANDBOX_ID -w '%{http_code}\n'
```

Expect `sandbox_user`/`sandbox` for the `select`, and `permission denied to create
role` for the `CREATE ROLE ... SUPERUSER` attempt.

**MySQL** (same shape, `mysql` has no native URI support so the DSN gets parsed into
separate flags):

```bash
curl -s http://localhost:8000/v1/sandboxes \
  -H 'Content-Type: application/json' \
  -d '{"language": "python", "template": "python-mysql-lab@1.0"}' | python3 -m json.tool
SANDBOX_ID=<paste the "id" from above>

read -r MYSQL_USER MYSQL_PW MYSQL_HOST MYSQL_PORT MYSQL_DB <<< $(curl -s http://localhost:8000/v1/sandboxes/$SANDBOX_ID/runs \
  -H 'Content-Type: application/json' \
  -d '{"code": "import os\nfrom urllib.parse import urlparse\nu = urlparse(os.environ[\"DATABASE_URL\"])\nprint(u.username, u.password, u.hostname, u.port, u.path.lstrip(\"/\"))"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["stdout"].strip())')

docker exec "kubesandbox-${SANDBOX_ID}-mysql" mysql -u "$MYSQL_USER" -p"$MYSQL_PW" -h 127.0.0.1 "$MYSQL_DB" -e "SELECT CURRENT_USER(), DATABASE();"
docker exec "kubesandbox-${SANDBOX_ID}-mysql" mysql -u "$MYSQL_USER" -p"$MYSQL_PW" -h 127.0.0.1 "$MYSQL_DB" -e "CREATE USER 'escalated'@'%' IDENTIFIED BY 'Str0ngPassword1';"

curl -s -X DELETE http://localhost:8000/v1/sandboxes/$SANDBOX_ID -w '%{http_code}\n'
```

Expect `sandbox_user@%`/`sandbox` for the `SELECT`, and "Access denied; you need (at
least one of) the CREATE USER privilege(s)" for the `CREATE USER` attempt.

**Redis** (no SQL-style role model — `on_provision` creates a scoped ACL user, then
disables the previously-unauthenticated default user):

```bash
curl -s http://localhost:8000/v1/sandboxes \
  -H 'Content-Type: application/json' \
  -d '{"language": "python", "template": "python-redis-lab@1.0"}' | python3 -m json.tool
SANDBOX_ID=<paste the "id" from above>

DSN=$(curl -s http://localhost:8000/v1/sandboxes/$SANDBOX_ID/runs \
  -H 'Content-Type: application/json' \
  -d '{"code": "import os\nprint(os.environ[\"DATABASE_URL\"])"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["stdout"].strip())')

docker exec "kubesandbox-${SANDBOX_ID}-redis" redis-cli -u "$DSN" ACL WHOAMI
docker exec "kubesandbox-${SANDBOX_ID}-redis" redis-cli -u "$DSN" FLUSHALL
docker exec "kubesandbox-${SANDBOX_ID}-redis" redis-cli PING   # unauthenticated — should fail

curl -s -X DELETE http://localhost:8000/v1/sandboxes/$SANDBOX_ID -w '%{http_code}\n'
```

Expect `sandbox_user` for `ACL WHOAMI`, `NOPERM ... no permissions to run the
'flushall' command` for `FLUSHALL`, and `NOAUTH Authentication required` for the
unauthenticated `PING` (confirming the default user really was disabled).

### Try the build system (Phase 6)

`BuildManager` (`app/services/build_manager.py`) turns a component's declared
`source.type` (`dockerfile`/`compose`/`pipeline`/`helm`, doc §8) into a real, pushed,
runnable golden image — closing the gap every prior phase's components sidestepped by
using `source.type: image` and a manual `docker build`. Four new demo components, one
per strategy, prove this end to end:

| Component | `source.type` | What it proves |
|---|---|---|
| `jq` | `dockerfile` | plain Dockerfile build via `DockerfileBuildStrategy`, pushed to the local registry |
| `ripgrep` | `compose` | `ComposeBuildStrategy` parses `docker-compose.yaml`, builds/tags the declared service |
| `httpie` | `pipeline` | `PipelineBuildStrategy` runs ordered steps, then packages the image; a build cache backed by the new `MinIOStorageProvider` skips the steps on an unchanged rebuild |
| `demo-echo` | `helm` | `HelmChartStrategy` renders a chart, storing the manifest as a `MinIOStorageProvider` artifact (not deployed — see "Known scope boundaries" below) |

A build runs in the background (`POST` returns `202` immediately) — poll
`GET /v1/builds/{id}` until `status` is `succeeded`/`failed`:

```bash
# Trigger a build (admin-only for public components, like publishing one)
BUILD_ID=$(curl -s -X POST http://localhost:8000/v1/components/jq/build \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# Poll until it succeeds
curl -s http://localhost:8000/v1/builds/$BUILD_ID | python3 -m json.tool

# Once succeeded, the built image is immediately runnable — no restart needed,
# BuildManager writes straight into the live Registry.built_images
curl -s http://localhost:8000/v1/execute \
  -H 'Content-Type: application/json' \
  -d '{"language": "jq", "code": "{\"hello\": \"world\"}"}' \
  | python3 -m json.tool
```

To prove the local registry (`localhost:5000`, doc §8.1's ACR-shaped stand-in) is a
real pull-through, not just a formality — remove the daemon's cached copy of the
*pushed* tag and confirm `/v1/execute` above still works by actually pulling it back:

```bash
docker rmi localhost:5000/kubesandbox/jq:1.0
curl -s http://localhost:8000/v1/execute \
  -H 'Content-Type: application/json' \
  -d '{"language": "jq", "code": "{\"hello\": \"world\"}"}' \
  | python3 -m json.tool   # still works — DockerProvisioner pulled it back
```

Same pattern for `ripgrep` (compose) and `httpie` (pipeline — build it twice and
compare timing/logs; the second run should skip its steps entirely on a cache hit).
`demo-echo` (helm) has no `/v1/execute` step — its build result is a rendered manifest,
not an image:

```bash
BUILD_ID=$(curl -s -X POST http://localhost:8000/v1/components/demo-echo/build \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s http://localhost:8000/v1/builds/$BUILD_ID | python3 -m json.tool
# "artifact_ref" points at the rendered manifest's object-storage key (MinIO)
```

Integration tests for all of this
(`tests/integration/test_build_manager_{docker,helm}.py`) self-skip with a clear
reason if Docker/the local registry/`helm`/MinIO aren't reachable — same pattern as
`test_execute_docker.py`.

**Known scope boundaries** (see `docs/TASK_CHECKLIST.md`'s Phase 6 section for the
full detail):

- **No Kaniko/Kubernetes-Job build path.** `DockerfileBuildStrategy` only builds
  against the local Docker daemon (`local`'s documented fallback, doc §8.1) — a real
  Kaniko-via-K8s-Job path for `aks-prod` isn't implemented this phase, the same
  honesty gap Phase 3 already carries for untested gVisor.
- **`ACRRegistryProvider`/`AzureBlobStorageProvider` are real implementations, not
  exercised live.** No Azure credentials/environment are available in this repo's
  dev setup — only `LocalImageStore`/`MinIOStorageProvider` are actually
  live-verified.
- **`ComposeBuildStrategy` builds each service's image — it doesn't auto-translate a
  multi-service compose file into `SidecarSpec`s.** Phase 5's hand-authored templates
  already cover real sidecar composition.
- **`HelmChartStrategy` renders and stores a manifest — it doesn't wire it into a
  running sandbox pod.** No existing doc section describes composing a helm-rendered
  service into a `SidecarSpec`.

**Troubleshooting: `exec /usr/bin/sleep: operation not permitted` / a sandbox
container exits immediately.** If `docker logs` on a just-created sandbox container
shows this, and `journalctl -k | grep apparmor` shows a `DENIED` line naming
`profile="snap.docker.dockerd"`, your Docker is installed via **snap**
(`snap list | grep docker`) — its own AppArmor confinement conflicts with the
`no-new-privileges` flag every sandbox container gets (doc §6 Layer 1; this isn't
optional, it's the same hardening Kubernetes' `allowPrivilegeEscalation: false` maps
to). This is a host Docker-packaging issue, not a KubeSandbox bug — confirmed by
reproducing it against a completely vanilla `debian:12-slim` with no other flags
applied. Fix: install Docker via the
[official apt-based Engine install](https://docs.docker.com/engine/install/) instead
of `snap install docker`.

### Browse the registry

```bash
curl -s http://localhost:8000/v1/components | python3 -m json.tool
curl -s http://localhost:8000/v1/components/python | python3 -m json.tool
curl -s http://localhost:8000/v1/templates | python3 -m json.tool
```

With `auth.disabled: true` (local default) the request runs as an admin, so every
component/template is visible unfiltered (doc §3.6). Registering a component/template
(`POST /v1/components`, `POST /v1/templates`) and managing entitlements/publish-grants
(`GET/PATCH /v1/admin/entitlements`, `GET/PATCH /v1/admin/publish-grants`) follow the
same pattern — see `app/api/v1/{components,templates,admin}.py`.

## Try the Kubernetes provisioner (Phase 3)

`KubernetesProvisioner` (`app/provisioners/kubernetes.py`) is a drop-in alternative to
`DockerProvisioner` — same `Provisioner` interface, so `SandboxService`/`/v1/execute`
don't change at all. It provisions one **namespace per sandbox** (holding a default-deny
NetworkPolicy, a ResourceQuota, a LimitRange, and the sandbox Pod itself) instead of a
bare container, and execs into it over the real Kubernetes streaming API.

```bash
# 1. Install kubectl + kind (no sudo needed — plain binaries)
mkdir -p ~/.local/bin
curl -sLo ~/.local/bin/kubectl "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
curl -sLo ~/.local/bin/kind "https://kind.sigs.k8s.io/dl/v0.27.0/kind-linux-amd64"
chmod +x ~/.local/bin/kubectl ~/.local/bin/kind
export PATH="$HOME/.local/bin:$PATH"

# 2. Create a kind cluster (pinned node image, see deploy/kind/kind-config.yaml)
kind create cluster --config deploy/kind/kind-config.yaml

# 3. Apply the sandbox-primitive Kustomize manifests (NetworkPolicy/RBAC/ResourceQuota/
#    LimitRange/RuntimeClass) — this is the control plane's own namespace, separate
#    from the per-sandbox namespaces KubernetesProvisioner creates dynamically
kubectl apply -k deploy/overlays/local

# 4. Load each golden image into the kind node (it doesn't share the host Docker cache)
#    — reload after any Dockerfile rebuild (e.g. Phase 4's dtach addition); kind caches
#    by image id, not by tag, so a stale in-cluster image otherwise silently persists.
kind load docker-image kubesandbox/python:3.12.4-slim --name kubesandbox-dev
kind load docker-image kubesandbox/node:20.15.0-slim kubesandbox/go:1.22.5-slim kubesandbox/base:1.0 --name kubesandbox-dev

# 5. Run the app with the Kubernetes backend instead of Docker (everything else about
#    local config — Postgres/Redis/MinIO — is unchanged; this only overrides the
#    provisioner)
KUBESANDBOX_PROVISIONER__BACKEND=kubernetes uv run uvicorn app.main:app --reload

# 6. Same /v1/execute call as above now runs in a real per-sandbox K8s namespace
curl -s http://localhost:8000/v1/execute \
  -H 'Content-Type: application/json' \
  -d '{"language": "python", "code": "print(\"hello from the k8s sandbox\")"}' \
  | python3 -m json.tool
```

`kubectl get namespaces` should show no `kubesandbox-sb-...` namespace left over a few
seconds after the run completes — namespace deletion is asynchronous in Kubernetes
(`Terminating` → gone), so allow a short moment before treating one as leaked.

Note: `runtimeClassName: gvisor` is only ever set when `provisioner.runtime_class` is
configured (`config/settings/aks-prod.yaml`) — `local.yaml` leaves it `null`, and a
plain kind node has no gVisor containerd shim anyway, so gVisor itself is never
exercised locally; only the wiring (the config is read and consumed) is.

## Tests

```bash
uv run pytest
```

Unit tests need no infra. The Docker integration test
(`tests/integration/test_execute_docker.py`) needs both the `python` golden image built
(step 3 above) and a working Docker socket — it skips itself with a clear reason if
either is unavailable. The Kubernetes integration test
(`tests/integration/test_execute_kubernetes.py`) similarly needs a reachable kind
cluster (see above) with the `python` golden image loaded into it — same
skip-with-a-clear-reason behavior if it isn't. `tests/integration/test_build_manager_
{docker,helm}.py` (Phase 6) self-skip the same way if Docker/the local
registry/`helm`/MinIO aren't reachable.

## Layout

See `docs/ARCHITECTURE_AND_PLAN.md` §18 for the full intended repository layout; this
slice implements the Phase 0–6 subset of it (config, manifests/registry, domain models,
DB + migrations, the `python`/`node`/`go`/`bash`/`git`/`base` components,
`DockerProvisioner` and `KubernetesProvisioner`, `SandboxService` (ad-hoc + SandboxTemplate
composition + the non-ephemeral sandbox lifecycle), `/v1/execute`, `/v1/sandboxes`
(create/status/destroy/runs/files/tree), `WS /v1/sandboxes/{id}/attach` (interactive
PTY), the component/template/entitlement/publish-grant registry APIs, the Kustomize
sandbox-primitive manifests under `deploy/`, database sidecars
(`components/databases/{postgresql,mysql,redis}`, multi-container composition in both
provisioners, and the `ComponentHook` loader that provisions each sidecar's scoped
role), and the build system (`app/build/strategies/{dockerfile,compose,pipeline,helm}.py`,
`app/services/build_manager.py`, `app/cloud/{registry,storage}.py`,
`POST /v1/components/{name}/build` + `GET /v1/builds/{id}`, and the
`jq`/`ripgrep`/`httpie`/`demo-echo` demo components — one per strategy). Everything
else in that layout (pooling, billing, execution-time entitlement enforcement, real
Kaniko/K8s-Job builds) is later-phase work per the roadmap in §20 — see
`docs/TASK_CHECKLIST.md` for the exact per-item status and known gaps.
