# kubesandbox-sdk

Python client for the KubeSandbox control plane — the workflow-builder's "code block"
talks to KubeSandbox through this (`docs/ARCHITECTURE_AND_PLAN.md` §17).

## Install

```bash
pip install kubesandbox-sdk            # batch execution + sandbox lifecycle + catalog
pip install 'kubesandbox-sdk[attach]'  # adds interactive PTY attach (needs websockets)
```

`httpx` is the only required dependency. This package intentionally shares no code with
the control plane's `app/` tree, so installing it never drags in FastAPI, SQLAlchemy,
the Kubernetes client, or the Azure SDKs.

## Batch execution — the workflow-builder path

One call, one bundled result. `stdin` is fed entirely up front and then closed (§5.1);
there is no live stdin wait on this path, and no streamed output — just "done or not".

```python
from kubesandbox import KubeSandboxClient

with KubeSandboxClient("https://kubesandbox.internal", api_key="ks_live_...") as ks:
    result = ks.execute(
        language="python",
        code="import sys; total = sum(int(x) for x in sys.stdin); print(total)",
        stdin="1\n2\n3\n",
    )
    print(result.exit_code)   # 0
    print(result.stdout)      # "6\n"
    print(result.variables)   # {"total": 6, ...} — §5.3's variable dump
    assert result.ok          # exit 0 AND not timed out AND not truncated
```

`result.ok` is stricter than `exit_code == 0` on purpose: a timed-out or output-truncated
run has incomplete stdout, and treating it as success is how a workflow step silently
acts on half an answer.

## A longer-lived sandbox, several runs

```python
with ks.sandbox(language="python") as sb:          # destroyed on the way out, always
    ks.upload_file(sb.id, "data.csv", csv_text)
    first = ks.run(sb.id, code="import csv, pathlib; ...")
    second = ks.run(sb.id, code="print('reusing the same warm sandbox')")
    for entry in ks.list_tree(sb.id):
        print(entry.path, entry.is_dir)
```

Without the context manager, a sandbox you forget to `destroy_sandbox()` sits there
until its idle TTL reaps it (§4.1) — and bills for that whole time when billing is on
(§13).

## Async

`AsyncKubeSandboxClient` mirrors the whole surface:

```python
from kubesandbox import AsyncKubeSandboxClient

async with AsyncKubeSandboxClient(url, api_key=key) as ks:
    result = await ks.execute(language="node", code="console.log(1+1)")
```

## Interactive PTY (standalone users, §5.2)

Requires the `attach` extra. Exactly one viewer per sandbox — a second concurrent
attach raises `ConflictError`, it is never multiplexed (no collaboration in v1).

```python
import sys
from kubesandbox import AsyncKubeSandboxClient
from kubesandbox.attach import attach

async with AsyncKubeSandboxClient(url, api_key=key) as ks:
    sb = await ks.create_sandbox(language="bash")
    async with await attach(ks, sb.id) as pty:
        await pty.resize(cols=120, rows=40)
        await pty.send_stdin(b"ls -la /workspace\n")
        async for event in pty:
            if event.kind == "exit":
                break
            sys.stdout.buffer.write(event.data)   # raw bytes; decode incrementally
```

`event.data` is bytes, not str: a PTY can split a multi-byte UTF-8 sequence across
chunks, so the SDK hands over the raw stream rather than guessing.

## Catalog and builds

```python
for component in ks.list_components(category="language"):
    print(component.key, component.display_name)

build = ks.trigger_build("jq")
build = ks.wait_for_build(build.id)      # polls until succeeded/failed
print(build.status, build.image_ref, build.error)
```

Catalog listings are entitlement-filtered server-side (§3.6): you see what an admin has
entitled your tenant to, not the whole registry. A *failed* build is returned, not
raised — inspect `build.error` / `build.log_excerpt`; only the poll timing out raises.

## Login and identity

Two ways to authenticate. A workflow-builder uses an API key; a human/UI signs in with
OIDC and exchanges the IdP's token for a short-lived KubeSandbox session token.

```python
# What IdP should the UI send the user to? (no credential needed for this one)
config = ks.auth_config()          # {issuer, client_id, scopes, session_ttl_seconds, ...}

# After your own OIDC/MSAL flow, trade the IdP token for a session token
session = ks.exchange_oidc_token(id_token_from_msal)
authed = KubeSandboxClient(url, api_key=None, headers={
    "Authorization": f"Bearer {session['access_token']}"
})

# Who am I, and what's turned on in this deployment?
identity = authed.me()
print(identity.principal.role)                     # admin | operator | user | service
print(identity.features.persistent_workspaces)     # gate your UI on these
```

Check `identity.features` before offering a feature: each flag is opt-in server-side
config, and calling into a disabled one returns a 400 rather than degrading gracefully.

## Listings and history

Growth-unbounded collections are paginated and return a `Page`:

```python
page = ks.list_sandboxes(state="active", limit=25)
print(page.total, page.has_more)
for sb in page.items:
    print(sb.id, sb.state, sb.created_at)

runs = ks.list_runs(sandbox_id=sb.id)
builds = ks.list_builds(status="failed")
keys = ks.list_api_keys()
usage = ks.list_usage(since_days=7)
```

Catalog listings (`list_components`, `list_templates`) return plain lists — they're
bounded by the git-committed registry, not by traffic.

## Non-blocking runs

When your own HTTP timeout is tighter than the sandbox's wall-clock cap, or you want to
show progress:

```python
run_id = ks.execute_async(language="python", code=long_running_code)
record = ks.wait_for_run(run_id)        # polls until completed/failed
if record.ok:
    print(record.as_result().stdout)    # same bundled shape as execute()
else:
    print(record.status, record.error)
```

A *failed* run is returned, not raised — only the poll timing out raises.

## API keys

Mint the key your workflow-builder will use. The plaintext is returned **once**:

```python
created = ks.create_api_key("workflow-builder prod")
print(created.api_key)          # store it now — only its hash is kept server-side
print(created.prefix)           # what listings show, so you can match it later

for key in ks.list_api_keys().items:
    print(key.label, key.prefix, key.revoked, key.last_used_at)

ks.revoke_api_key(created.id)   # idempotent
```

`last_used_at` is what makes "is this key still needed?" answerable before revoking.

## Billing and workspace

```python
account = ks.billing_account()
print(account.mode, account.balance, account.month_to_date_cost)

status = ks.my_workspace()
if not status.enabled:
    ...                          # persistence is off in this deployment
elif status.workspace is None:
    ...                          # none created yet — the first persistent sandbox makes one
else:
    print(status.workspace.used_mb, status.workspace.quota_mb, status.workspace.state)
```

Three distinct workspace states, deliberately not collapsed into a 404: "off here",
"none yet", and "here it is" call for different UI.

## Errors

Every non-2xx becomes a typed exception (all subclass `KubeSandboxAPIError`, which
subclasses `KubeSandboxError`):

| Exception | Status | Typical cause |
|---|---|---|
| `BadRequestError` | 400 / 422 | Unknown language, template/version conflict, persistence not enabled here |
| `AuthenticationError` | 401 | Missing, invalid, or revoked API key |
| `PermissionDeniedError` | 403 | Not entitled to that component/template; admin-only endpoint |
| `NotFoundError` | 404 | No such sandbox/component/build — also what another tenant's id looks like |
| `ConflictError` | 409 | A second viewer tried to attach |
| `QuotaExceededError` | 429 | Quota, insufficient credit, or spend cap (§13) |
| `ProvisionerError` | 502 | Docker/Kubernetes backend failed — retryable in principle |
| `ServiceUnavailableError` | 503 | That replica reported itself not ready |

`.status_code` and `.detail` (the server's own message) are on every one of them.

After a `QuotaExceededError`, `ks.request_credit(amount=..., reason=...)` files a
credit/headroom request for an admin to review — it grants nothing by itself.

## What this SDK deliberately does not cover

- **Admin endpoints** (`/v1/admin/*`: entitlements, publish grants, billing mode,
  pricing rules, wallet top-ups, credit-request review). Those are operator tooling for
  a human with the admin role, not something a workflow-builder should be one typo away
  from calling. Use `curl`/the OpenAPI docs at `/docs`.
- **Component/template publishing** (`POST /v1/components`, `POST /v1/templates`).
  Manifests live in git as the source of truth (§3.5); publishing from an application
  at runtime is the wrong direction.
- **Retries.** No automatic retry on 502/503: `execute()` is not idempotent unless the
  caller supplies an idempotency key, and blindly retrying a run that may have already
  executed is worse than surfacing the error. Retry deliberately, at your level.
