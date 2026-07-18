# KubeSandbox

Sandbox-provisioning control plane. See `docs/ARCHITECTURE_AND_PLAN.md` for the full
design, and `docs/TASK_CHECKLIST.md` for an honest per-item completion status. This
covers local setup through the Phase 0–2 slice: config, DB, the `python`/`node`/`go`/
`bash`/`git`/`base` components, `DockerProvisioner`, `POST /v1/execute` (ad-hoc language
or SandboxTemplate composition), and the component/template/entitlement registry APIs.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (manages the venv/deps — always run Python via
  `uv run ...`, not a bare `python3`, so the right interpreter/deps are used)
- Docker, with your user in the `docker` group (`sudo usermod -aG docker $USER`, then
  log out/in or `newgrp docker`) — `docker ps` should work without `sudo`

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

## Tests

```bash
uv run pytest
```

Unit tests need no infra. The Docker integration test
(`tests/integration/test_execute_docker.py`) needs both the `python` golden image built
(step 3 above) and a working Docker socket — it skips itself with a clear reason if
either is unavailable.

## Layout

See `docs/ARCHITECTURE_AND_PLAN.md` §18 for the full intended repository layout; this
slice implements the Phase 0–2 subset of it (config, manifests/registry, domain models,
DB + migrations, the `python`/`node`/`go`/`bash`/`git`/`base` components,
`DockerProvisioner`, `SandboxService` (ad-hoc + SandboxTemplate composition),
`/v1/execute`, and the component/template/entitlement/publish-grant registry APIs).
Everything else in that layout (Kubernetes provisioner, interactive PTY, database
sidecars, the real build system, pooling, billing, execution-time entitlement
enforcement) is later-phase work per the roadmap in §20 — see
`docs/TASK_CHECKLIST.md` for the exact per-item status and known gaps.
