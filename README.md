# KubeSandbox

Sandbox-provisioning control plane. See `docs/ARCHITECTURE_AND_PLAN.md` for the full
design, and `docs/TASK_CHECKLIST.md` for an honest per-item completion status. This
covers local setup through the Phase 0–5 slice: config, DB, the `python`/`node`/`go`/
`bash`/`git`/`base` components, `DockerProvisioner` **and** `KubernetesProvisioner`,
`POST /v1/execute` (ad-hoc language or SandboxTemplate composition), the non-ephemeral
sandbox lifecycle (`POST /v1/sandboxes`, batch runs, file upload/download/tree, and
interactive PTY attach over WebSocket), the component/template/entitlement registry
APIs, the Kustomize sandbox-primitive manifests (NetworkPolicy/RBAC/ResourceQuota/
LimitRange/RuntimeClass), and database sidecars (`postgresql`/`mysql`/`redis`
composed into a sandbox as a real second container, with a non-superuser role/ACL user
provisioned by a `ComponentHook` — see "Try a database sidecar" below; all three are
live-verified end to end against real containers, see `docs/TASK_CHECKLIST.md`'s
Phase 5 section).

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

## One-time setup

```bash
# 1. Install dependencies into .venv
uv sync

# 2. Start supporting infra (Postgres, Redis, MinIO, local image registry)
docker compose up -d

# 3. Build each component's golden image (BuildManager/Kaniko is Phase 6 — for now
#    these are one-time manual builds, matching docs §8.1's local fallback). The tags
#    must match each component.yaml's spec.source.image exactly. All four now include
#    `dtach` (Phase 4 interactive attach reattach) — if you built these before Phase 4,
#    rebuild them, there's no other way to pick up the Dockerfile change.
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
multi-component template, runnable today because all three share one pre-baked image
(true separate-image merging needs BuildManager, Phase 6 — see
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
skip-with-a-clear-reason behavior if it isn't.

## Layout

See `docs/ARCHITECTURE_AND_PLAN.md` §18 for the full intended repository layout; this
slice implements the Phase 0–5 subset of it (config, manifests/registry, domain models,
DB + migrations, the `python`/`node`/`go`/`bash`/`git`/`base` components,
`DockerProvisioner` and `KubernetesProvisioner`, `SandboxService` (ad-hoc + SandboxTemplate
composition + the non-ephemeral sandbox lifecycle), `/v1/execute`, `/v1/sandboxes`
(create/status/destroy/runs/files/tree), `WS /v1/sandboxes/{id}/attach` (interactive
PTY), the component/template/entitlement/publish-grant registry APIs, the Kustomize
sandbox-primitive manifests under `deploy/`, and database sidecars
(`components/databases/{postgresql,mysql,redis}`, multi-container composition in both
provisioners, and the `ComponentHook` loader that provisions each sidecar's scoped
role). Everything else in that layout (the real build system, pooling, billing,
execution-time entitlement enforcement) is later-phase work per the roadmap in §20 —
see `docs/TASK_CHECKLIST.md` for the exact per-item status and known gaps.
