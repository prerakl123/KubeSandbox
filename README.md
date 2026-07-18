# KubeSandbox

Sandbox-provisioning control plane. See `docs/ARCHITECTURE_AND_PLAN.md` for the full
design. This covers local setup for the Phase 0/1 slice: config, DB, the `python`
component, `DockerProvisioner`, and `POST /v1/execute`.

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

# 3. Build the Python component's golden image (BuildManager/Kaniko is Phase 6 —
#    for now this is a one-time manual build, matching docs §8.1's local fallback)
docker build -t kubesandbox/python:3.12.4-slim components/languages/python

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

## Tests

```bash
uv run pytest
```

Unit tests need no infra. The Docker integration test
(`tests/integration/test_execute_docker.py`) needs both the golden image built (step 3
above) and a working Docker socket — it skips itself with a clear reason if either is
unavailable.

## Layout

See `docs/ARCHITECTURE_AND_PLAN.md` §18 for the full intended repository layout; this
slice implements the Phase 0/1 subset of it (config, manifests/registry, domain models,
DB + migrations, the `python` component, `DockerProvisioner`, `SandboxService`,
`/v1/execute`). Everything else in that layout (Kubernetes provisioner, interactive PTY,
entitlements enforcement, pooling, billing, other components) is later-phase work per
the roadmap in §20.
