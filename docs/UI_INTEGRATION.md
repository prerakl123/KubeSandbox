# KubeSandbox — UI integration guide

What a frontend needs from the control plane, and what it must not assume. Written for
the "standalone users" consumer in `ARCHITECTURE_AND_PLAN.md` §1 — the human at a
terminal, not the workflow-builder (which is batch-only and talks through `sdk/`).

Building the UI is a separate stage of work. This document is the contract it will build
against, and the honest list of what the backend does and doesn't provide as of Phase 9.

---

## 1. Before anything: two settings block a UI outright

| Setting | Symptom if unset | Where |
|---|---|---|
| `cors.enabled` + `cors.allow_origins` | Every request fails in the browser before reaching the API — the browser rejects the preflight. Nothing appears in server logs. | `config/settings/*.yaml`, or the Helm chart's `config.cors` |
| `auth.oidc_issuer` + `oidc_audience` + `oidc_client_id` | `POST /v1/auth/token` returns **503** and browser login is impossible; only API keys work. | same |

Both are off/empty by default, deliberately: a permissive CORS default on a service that
hands out sandbox sessions is the wrong default, and a half-configured OIDC setup should
fail loudly rather than silently accept nothing.

In local development, `auth.disabled: true` (refused outside `app_env=local`) makes every
request the local admin principal, so a UI can be developed with no IdP at all — but
CORS still applies. Set `cors.enabled: true` with `http://localhost:5173` (or whatever
your dev server uses) in `config/settings/local.yaml`.

---

## 2. Login flow

The IdP's token is validated **once**, exchanged for a short-lived KubeSandbox session
token, and that token is what every subsequent request carries (§11's
"OIDC → short-lived JWT session").

```
  UI                          KubeSandbox                    Azure AD
  │                                │                             │
  ├─ GET /v1/auth/config ─────────►│  (no credential needed)     │
  │◄─ issuer, client_id, scopes ───┤                             │
  │                                │                             │
  ├─ MSAL interactive login ───────┼────────────────────────────►│
  │◄─ id_token ────────────────────┼─────────────────────────────┤
  │                                │                             │
  ├─ POST /v1/auth/token ─────────►│─ validate vs. JWKS ────────►│
  │   {oidc_token}                 │◄─ signing keys ─────────────┤
  │◄─ {access_token, expires_in,   │  provision tenant+user      │
  │     principal}                 │  on first login             │
  │                                │                             │
  ├─ GET /v1/me ──────────────────►│  Authorization: Bearer ...  │
  │◄─ principal + features ────────┤                             │
```

**Renewal**: there is deliberately no refresh-token flow. `expires_in` (1 hour by
default) is on the token response and `session_ttl_seconds` on `/v1/auth/config` — run a
silent MSAL re-auth and call `/v1/auth/token` again before it lapses. The IdP already
holds the long-lived session; duplicating that here would add a second thing to revoke.

**Roles**: a first-time user is always created with role `user`. No IdP claim can grant
`admin` — an existing admin must promote them via
`PATCH /v1/admin/users/{id}/role`. Note this means **the very first admin has to be
created by a direct DB write** (or by running locally with `auth.disabled`, where the
local-dev principal is an admin).

---

## 3. Drive the UI from `GET /v1/me`

```json
{
  "principal": { "tenant_id": "…", "user_id": "…", "role": "user", "email": "a@x.com" },
  "features": {
    "persistent_workspaces": true,
    "billing": false,
    "pooling": true,
    "interactive_attach": true
  },
  "app_env": "aks-prod"
}
```

Every flag in `features` is opt-in server-side config. Rendering a persistent-workspace
toggle or a credit balance against a deployment where the feature is off produces
controls that only ever return 400 — check the flag instead of assuming.

`role` drives the admin surface: `admin` sees `/v1/admin/*`, everyone else gets 403.
`role: "service"` means an API key is calling, which should not happen from a UI.

---

## 4. The interactive terminal

`WS /v1/sandboxes/{id}/attach`. The credential goes in the **query string**, because a
browser cannot set headers on a WebSocket handshake:

```js
const ws = new WebSocket(
  `${wsBase}/v1/sandboxes/${id}/attach?access_token=${sessionToken}`
);
```

Use the session token, not an API key: a 1-hour token in a URL is a bounded exposure; a
long-lived key is not. Use `wss://` anywhere but local dev, so the query string is inside
TLS.

Frames are JSON text with base64 payloads:

| Direction | Frame |
|---|---|
| → server | `{"type":"stdin","data":"<b64>"}` |
| → server | `{"type":"resize","cols":120,"rows":40}` |
| → server | `{"type":"signal","signal":"SIGINT"\|"SIGQUIT"\|"SIGTSTP"}` |
| ← client | `{"type":"stdout","data":"<b64>"}` |
| ← client | `{"type":"exit","exit_code":0}` |
| ← client | `{"type":"error","message":"…"}` |

Three things that will bite a naive implementation:

1. **One viewer per sandbox.** A second concurrent attach is rejected with a real HTTP
   **409** during the handshake, not multiplexed. There is no collaboration in v1.
2. **Reattach works; the grace window is ~30s.** On disconnect the viewer's claim is
   left to expire, so the *same* identity reconnecting within that window is let straight
   back in. A *different* identity is blocked until it lapses.
3. **`stdout` is a byte stream, not text.** A PTY can split a multi-byte UTF-8 sequence
   across frames. Feed the decoded bytes to an incremental decoder (or to xterm.js, which
   handles this) — decoding each frame independently will produce replacement characters
   at chunk boundaries.

Signals travel as terminal control bytes, which is why only SIGINT/SIGQUIT/SIGTSTP exist.
SIGKILL/SIGTERM have no representation on a PTY: destroy the sandbox instead.

Attach requires a sandbox from `POST /v1/sandboxes`. `POST /v1/execute`'s sandbox is
ephemeral and already gone by the time it returns.

---

## 5. Running code

Two shapes, and the UI will want both:

**Blocking** — `POST /v1/execute`. Returns the bundled result. Simple, but the request is
open for the whole run (up to the 60s wall-clock cap), which makes a progress indicator
impossible and is fragile behind a proxy with a short read timeout.

**Non-blocking** — `POST /v1/execute?async=true` → `202 {run_id, status}`, then poll
`GET /v1/runs/{run_id}` until `status` is `completed` or `failed`. The terminal response
carries the same bundled body the blocking call would have returned.

Be clear about what async does **not** give you: there is no incremental output on either
path, only "done vs. not done yet" (§5.1). Streaming a run's output as it happens is what
the interactive PTY is for. If a UI wants live output for a *batch* run, the answer is to
create a sandbox and attach, not to poll harder.

`stdin` is supplied entirely up front and then closed. A program that reads past it gets
EOF immediately — there is no way for a UI to send more stdin to a batch run.

---

## 6. Endpoint map

Everything a UI needs, grouped by the screen that consumes it.

| Screen | Endpoints |
|---|---|
| Login | `GET /v1/auth/config`, `POST /v1/auth/token` |
| Shell / nav | `GET /v1/me` |
| Environment picker | `GET /v1/components`, `GET /v1/templates`, `GET /v1/templates/{name}` |
| Editor / run | `POST /v1/execute` (± `?async=true`), `GET /v1/runs/{run_id}` |
| Sandbox dashboard | `GET /v1/sandboxes`, `POST /v1/sandboxes`, `GET`/`DELETE /v1/sandboxes/{id}` |
| Terminal | `WS /v1/sandboxes/{id}/attach` |
| File browser | `GET /v1/sandboxes/{id}/tree`, `GET`/`PUT /v1/sandboxes/{id}/files` |
| Run history | `GET /v1/runs` |
| Storage | `GET /v1/workspaces/me` |
| Billing | `GET /v1/billing/account`, `GET /v1/billing/usage`, `POST`/`GET /v1/billing/credit-requests` |
| Settings / keys | `POST`/`GET`/`DELETE /v1/api-keys` |
| Builds | `GET /v1/builds`, `GET /v1/builds/{id}`, `POST /v1/components/{name}/build` |
| Admin | `GET /v1/admin/tenants`, `GET /v1/admin/users`, `PATCH /v1/admin/users/{id}/role`, `GET`/`PATCH /v1/admin/entitlements`, `GET`/`PATCH /v1/admin/publish-grants`, `GET`/`POST /v1/admin/pricing-rules`, `PATCH /v1/admin/tenants/{id}/billing`, `POST /v1/admin/tenants/{id}/credit`, `GET /v1/admin/credit-requests`, `PATCH /v1/admin/credit-requests/{id}` |

The live OpenAPI spec at `/openapi.json` (Swagger UI at `/docs`) is authoritative — every
endpoint, parameter, and response field carries an inline description, so it can generate
a typed client directly.

---

## 7. Pagination

Collections that grow with use return an envelope:

```json
{ "items": [...], "total": 812, "limit": 50, "offset": 0 }
```

`limit` is 1–200 (default 50); out-of-range values are a **422**, not a silent clamp.
`total` ignores the window, so a pager can be sized. Ordering is always newest-first and
deterministic (a secondary id tiebreaker), so paging can't skip or repeat a row just
because two rows share a timestamp.

Paginated: sandboxes, runs, builds, API keys, usage records, credit requests, tenants,
users. **Not** paginated: `/v1/components`, `/v1/templates`, `/v1/admin/pricing-rules` —
bounded by the git-committed registry and by admin action, not by traffic.

Honest limitation: this is offset pagination, so a row can shift between pages if the
underlying set changes mid-paging. Acceptable for tenant-scoped views of thousands of
rows; a cursor API would be the answer if that stops being true.

---

## 8. Errors

Every error is `{"detail": "..."}`. Status codes a UI must handle distinctly:

| Status | Meaning | UI response |
|---|---|---|
| 400 | Domain error — unknown language, persistence not enabled here, service account has no workspace | Show `detail`; it is written to be read by a human |
| 401 | No/invalid/expired credential | Re-run the login flow |
| 403 | Not entitled, or admin-only | Hide the control rather than showing a failing button |
| 404 | Doesn't exist **or belongs to another tenant** | "Not found" — never distinguish the two |
| 409 | Sandbox already has a viewer | "Session open elsewhere" |
| 429 | Quota, insufficient credit, or spend cap | Offer `POST /v1/billing/credit-requests` |
| 502 | Provisioner failed | Retryable; offer a retry |
| 503 | Replica not ready, or OIDC not configured | Distinguish via which endpoint returned it |

**Tenant isolation is enforced as 404, never 403.** Another tenant's sandbox, run, or key
is reported as missing, so ids cannot be probed for existence. Don't build UI that treats
403 as "exists but forbidden".

---

## 9. What the backend does *not* provide

Being explicit, so the UI isn't designed around something that doesn't exist:

- **No incremental batch output.** See §5. Streaming means attach.
- **No push/subscribe.** No SSE, no WebSocket for anything but PTY attach. A dashboard
  showing live sandbox state must poll. Prefer `GET /v1/sandboxes/{id}` (which asks the
  provisioner and self-heals a stale row) over polling the list, which reports last-known
  DB state.
- **No collaboration.** One viewer per sandbox, by design.
- **No rate limiting.** Nothing throttles any endpoint yet, including credit requests. A
  UI should still debounce, but it will not be told to.
- **No audit log.** The `audit_logs` table exists and nothing writes to it, so there is no
  activity feed to render.
- **No quota enforcement beyond billing.** `QuotaService` (concurrent-sandbox caps,
  monthly minutes) is not implemented — a UI cannot show a sandbox quota because none is
  enforced.
- **No full run logs.** `GET /v1/runs/{id}` returns the first 10 KB of each stream, which
  is what's persisted. §10.1 puts overflow in object storage; nothing writes or serves it.
- **No notifications.** A filed credit request is a queued row; nobody is told. An admin
  screen has to poll `GET /v1/admin/credit-requests?status=pending`.
- **No async-run durability.** A control-plane restart mid-run leaves that run `running`
  forever. Nothing resumes or reaps it.
- **No user/tenant creation API.** Users are provisioned on first OIDC login; tenants are
  derived from the directory claim. There is no "invite a user" endpoint.

---

## 10. Local development setup

```bash
docker compose up -d postgres redis minio registry
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
```

With `config/settings/local.yaml`'s `auth.disabled: true`, every request is the local
admin — so the UI can skip login entirely and call `/v1/me` directly. Add your dev
server's origin to `cors.allow_origins` and set `cors.enabled: true` in that same file.

`/docs` for Swagger UI, `/openapi.json` to generate a typed client.
