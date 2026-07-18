# KubeSandbox

Sandbox-provisioning control plane. See `docs/ARCHITECTURE_AND_PLAN.md` for the full
design, and `docs/TASK_CHECKLIST.md` for an honest per-item completion status. This
covers local setup through the Phase 0–3 slice: config, DB, the `python`/`node`/`go`/
`bash`/`git`/`base` components, `DockerProvisioner` **and** `KubernetesProvisioner`,
`POST /v1/execute` (ad-hoc language or SandboxTemplate composition), the
component/template/entitlement registry APIs, and the Kustomize sandbox-primitive
manifests (NetworkPolicy/RBAC/ResourceQuota/LimitRange/RuntimeClass).

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
#    must match each component.yaml's spec.source.image exactly.
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
slice implements the Phase 0–3 subset of it (config, manifests/registry, domain models,
DB + migrations, the `python`/`node`/`go`/`bash`/`git`/`base` components,
`DockerProvisioner` and `KubernetesProvisioner`, `SandboxService` (ad-hoc + SandboxTemplate
composition), `/v1/execute`, the component/template/entitlement/publish-grant registry
APIs, and the Kustomize sandbox-primitive manifests under `deploy/`). Everything else in
that layout (interactive PTY, database sidecars, the real build system, pooling,
billing, execution-time entitlement enforcement) is later-phase work per the roadmap in
§20 — see `docs/TASK_CHECKLIST.md` for the exact per-item status and known gaps.
