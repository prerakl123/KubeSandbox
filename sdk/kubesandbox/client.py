"""`KubeSandboxClient` — the synchronous client, and the one doc §17 actually calls for:
"a thin client SDK (`sdk/`, Python first) wraps these for the workflow-builder's code
block."

A workflow step is synchronous and unattended (doc §1): it submits code, blocks, and
gets one bundled result. That's `execute()` below, and it's deliberately the shortest
path in this file. `AsyncKubeSandboxClient` in `async_client.py` mirrors this surface
for async callers and is what the PTY attach helper composes with.

Both clients accept an `httpx` transport so they can be pointed straight at an ASGI app
in tests — that's how `tests/unit/test_sdk.py` exercises the real routers without a
socket.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Iterator, Mapping

import httpx

from . import _transport as t
from .errors import KubeSandboxError
from .models import (
    ApiKeySummary,
    BatchRunResult,
    BillingAccount,
    Build,
    Component,
    CreatedApiKey,
    CreditRequest,
    FileEntry,
    Identity,
    Page,
    RunRecord,
    Sandbox,
    Template,
    UsageRecord,
    WorkspaceStatus,
)


class KubeSandboxClient:
    """Synchronous KubeSandbox control-plane client.

    ```python
    with KubeSandboxClient("https://kubesandbox.internal", api_key="...") as ks:
        result = ks.execute(language="python", code="print(sum(range(10)))")
        print(result.stdout)          # "45\\n"
        print(result.variables)       # doc §5.3's variable dump, when supported
    ```
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = t.DEFAULT_TIMEOUT,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """`client` lets a caller supply a fully-configured `httpx.Client` (custom
        proxies, mTLS, connection limits); when given, `base_url`/`timeout`/`headers`/
        `transport` are the caller's responsibility and this class only owns the
        endpoint logic. `transport` is the narrower hook used by the test suite to
        mount an ASGI app."""
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers=t.build_headers(api_key, headers),
            transport=transport,
        )

    # -- plumbing ---------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.request(method, path, **kwargs)

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return t.json_body(self._request(method, path, **kwargs))

    def close(self) -> None:
        """No-op for a caller-supplied client — closing something this object didn't
        open would break a client shared across several SDK instances."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> KubeSandboxClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- health -----------------------------------------------------------------------

    def healthz(self) -> dict:
        return self._json("GET", "/healthz")

    def readyz(self) -> dict:
        """Note that a not-ready replica answers 503, which this SDK raises as
        `ServiceUnavailableError` — catch it if you're polling for readiness rather
        than asserting it."""
        return self._json("GET", "/readyz")

    # -- batch execution (doc §5.1) ---------------------------------------------------

    def execute(
        self,
        *,
        language: str,
        code: str,
        stdin: str = "",
        version: str | None = None,
        template: str | None = None,
    ) -> BatchRunResult:
        """One-shot ephemeral batch run: acquire, run to completion, tear down.

        Blocks until the run finishes or hits its server-side wall-clock cap. `stdin`
        is fed entirely up front and then closed (doc §5.1) — a program that reads past
        it gets EOF immediately; there is no live client on this path to wait for.
        """
        body: dict[str, Any] = {"language": language, "code": code, "stdin": stdin}
        if version is not None:
            body["version"] = version
        if template is not None:
            body["template"] = template
        return BatchRunResult.from_dict(self._json("POST", "/v1/execute", json=body))

    # -- sandbox lifecycle (doc §17) --------------------------------------------------

    def create_sandbox(
        self,
        *,
        language: str,
        version: str | None = None,
        template: str | None = None,
        persistent: bool = False,
    ) -> Sandbox:
        body: dict[str, Any] = {"language": language, "persistent": persistent}
        if version is not None:
            body["version"] = version
        if template is not None:
            body["template"] = template
        return Sandbox.from_dict(self._json("POST", "/v1/sandboxes", json=body))

    def get_sandbox(self, sandbox_id: str) -> Sandbox:
        return Sandbox.from_dict(self._json("GET", f"/v1/sandboxes/{sandbox_id}"))

    def destroy_sandbox(self, sandbox_id: str) -> None:
        """Idempotent, like the server-side call it wraps — safe to call on an already
        destroyed sandbox."""
        t.raise_for_status(self._request("DELETE", f"/v1/sandboxes/{sandbox_id}"))

    def run(
        self, sandbox_id: str, *, code: str, stdin: str = "", language: str | None = None
    ) -> BatchRunResult:
        """Same bundled batch contract as `execute()`, against an existing warm sandbox.
        Never destroys it. `language` is only needed when the sandbox was created from a
        template with more than one runnable component."""
        body: dict[str, Any] = {"code": code, "stdin": stdin}
        if language is not None:
            body["language"] = language
        return BatchRunResult.from_dict(self._json("POST", f"/v1/sandboxes/{sandbox_id}/runs", json=body))

    @contextmanager
    def sandbox(
        self,
        *,
        language: str,
        version: str | None = None,
        template: str | None = None,
        persistent: bool = False,
    ) -> Iterator[Sandbox]:
        """Create a sandbox, hand it over, and always destroy it on the way out.

        The right shape for a multi-step workflow that needs several runs to share one
        warm sandbox — a leaked sandbox otherwise sits there until its idle TTL reaps
        it (doc §4.1), billing the whole time (doc §13).

        ```python
        with ks.sandbox(language="python") as sb:
            ks.upload_file(sb.id, "data.csv", csv_text)
            print(ks.run(sb.id, code="import csv; ...").stdout)
        ```
        """
        created = self.create_sandbox(
            language=language, version=version, template=template, persistent=persistent
        )
        try:
            yield created
        finally:
            self.destroy_sandbox(created.id)

    # -- workspace files (doc §5.4) ---------------------------------------------------

    def upload_file(self, sandbox_id: str, path: str, content: str) -> None:
        """`path` is relative to the sandbox's `/workspace` and must not escape it —
        the server rejects absolute or `..`-containing paths with a 400."""
        t.raise_for_status(
            self._request(
                "PUT",
                f"/v1/sandboxes/{sandbox_id}/files",
                params={"path": path},
                content=content.encode("utf-8"),
                headers={"Content-Type": "application/octet-stream"},
            )
        )

    def download_file(self, sandbox_id: str, path: str) -> bytes:
        response = self._request("GET", f"/v1/sandboxes/{sandbox_id}/files", params={"path": path})
        t.raise_for_status(response)
        return response.content

    def list_tree(self, sandbox_id: str, path: str = "") -> list[FileEntry]:
        payload = self._json("GET", f"/v1/sandboxes/{sandbox_id}/tree", params={"path": path})
        return [FileEntry.from_dict(entry) for entry in payload]

    # -- catalog (doc §3, entitlement-filtered per caller) ----------------------------

    def list_components(self, *, category: str | None = None) -> list[Component]:
        params = {"category": category} if category else None
        return [Component.from_dict(c) for c in self._json("GET", "/v1/components", params=params)]

    def get_component_versions(self, name: str) -> dict:
        """Returns the raw payload — `{name, versions, json_schema}` — because
        `json_schema` is the Component JSON Schema itself, for client-side manifest
        validation, and wrapping that in a dataclass would only get in the way."""
        return self._json("GET", f"/v1/components/{name}")

    def list_templates(self) -> list[Template]:
        return [Template.from_dict(x) for x in self._json("GET", "/v1/templates")]

    # -- builds (doc §8) --------------------------------------------------------------

    def trigger_build(self, name: str, *, version: str | None = None) -> Build:
        params = {"version": version} if version else None
        return Build.from_dict(self._json("POST", f"/v1/components/{name}/build", params=params))

    def get_build(self, build_id: str) -> Build:
        return Build.from_dict(self._json("GET", f"/v1/builds/{build_id}"))

    def wait_for_build(self, build_id: str, *, timeout: float = 1800.0, poll_interval: float = 5.0) -> Build:
        """Poll until the build reaches `succeeded`/`failed`, or raise on timeout.

        Returns the terminal record either way — a *failed* build is a legitimate
        result to inspect (`build.error`, `build.log_excerpt`), not an exception. Only
        running out of patience raises.
        """
        deadline = time.monotonic() + timeout
        while True:
            build = self.get_build(build_id)
            if build.done:
                return build
            if time.monotonic() >= deadline:
                raise KubeSandboxError(
                    f"build {build_id} still {build.status!r} after {timeout}s"
                )
            time.sleep(poll_interval)

    # -- billing self-service (doc §13) -----------------------------------------------

    def request_credit(self, *, amount: float, reason: str) -> CreditRequest:
        """File a request for more credit / spend-cap headroom. The path forward after
        a `QuotaExceededError` — it queues a row for an admin to review, and grants
        nothing by itself."""
        return CreditRequest.from_dict(
            self._json("POST", "/v1/billing/credit-requests", json={"amount": amount, "reason": reason})
        )

    def list_credit_requests(self) -> list[CreditRequest]:
        """Every request the caller's own tenant has filed, newest first — the server
        scopes this to the caller's tenant and takes no filters (the admin-side
        `GET /v1/admin/credit-requests` is the one with tenant/status filtering, and
        this SDK deliberately exposes no admin endpoints; see the README)."""
        payload = self._json("GET", "/v1/billing/credit-requests")
        return [CreditRequest.from_dict(x) for x in payload]

    # -- identity (Phase 9) -----------------------------------------------------------

    def auth_config(self) -> dict:
        """Public OIDC configuration for a frontend. The one endpoint that needs no
        credential — it's what a client calls before it has one."""
        return self._json("GET", "/v1/auth/config")

    def exchange_oidc_token(self, oidc_token: str) -> dict:
        """Trade an IdP token for a KubeSandbox session token (doc §11).

        Returns the raw payload (`access_token`, `expires_in`, `principal`) rather than a
        model, because the useful next step is constructing a new client with the token —
        this SDK deliberately does not mutate its own auth in place, since a client whose
        credential changes under concurrent use is a debugging nightmare.
        """
        return self._json("POST", "/v1/auth/token", json={"oidc_token": oidc_token})

    def me(self) -> Identity:
        """Who am I, and which optional features are enabled here. Worth calling once
        after authenticating: `identity.features` tells you whether persistence, billing,
        or pooling are on, and calling into a disabled one returns a 400."""
        return Identity.from_dict(self._json("GET", "/v1/me"))

    # -- listings (Phase 9) -----------------------------------------------------------

    def list_sandboxes(
        self, *, state: str | None = None, mine: bool = False, limit: int = 50, offset: int = 0
    ) -> Page:
        """This tenant's sandboxes, newest first. Includes terminated ones unless you
        filter — history, not just live sandboxes.

        Reports each sandbox's last-known state from the database; `get_sandbox` is the
        live-status call.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset, "mine": mine}
        if state is not None:
            params["state"] = state
        return Page.of(self._json("GET", "/v1/sandboxes", params=params), Sandbox.from_dict)

    def list_runs(
        self,
        *,
        sandbox_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        """Run history, newest first, without output bodies — fetch `get_run` for those."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if sandbox_id is not None:
            params["sandbox_id"] = sandbox_id
        if status is not None:
            params["status"] = status
        return Page.of(self._json("GET", "/v1/runs", params=params), RunRecord.from_dict)

    def get_run(self, run_id: str) -> RunRecord:
        """One run in full, including its bundled result once terminal (doc §5.1)."""
        return RunRecord.from_dict(self._json("GET", f"/v1/runs/{run_id}"))

    def list_builds(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> Page:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status is not None:
            params["status"] = status
        return Page.of(self._json("GET", "/v1/builds", params=params), Build.from_dict)

    def get_template(self, name: str) -> dict:
        """All visible versions of one template with their full resource/TTL shape.
        Returned raw: the detail payload is a `{name, versions}` wrapper whose versions
        carry more fields than `Template` models, and flattening it would lose them."""
        return self._json("GET", f"/v1/templates/{name}")

    # -- async execution (doc §5.1's ?async=true) --------------------------------------

    def execute_async(
        self,
        *,
        language: str,
        code: str,
        stdin: str = "",
        version: str | None = None,
        template: str | None = None,
    ) -> str:
        """Start a run without blocking and return its `run_id`.

        Use this when your own HTTP timeout is tighter than the sandbox's wall-clock cap,
        or when you want to show progress. An unknown language still fails *here*, not
        later in the background.
        """
        body: dict[str, Any] = {"language": language, "code": code, "stdin": stdin}
        if version is not None:
            body["version"] = version
        if template is not None:
            body["template"] = template
        return self._json("POST", "/v1/execute", params={"async": True}, json=body)["run_id"]

    def wait_for_run(
        self, run_id: str, *, timeout: float = 300.0, poll_interval: float = 1.0
    ) -> RunRecord:
        """Poll until the run is terminal, or raise on timeout.

        A *failed* run is returned, not raised — inspect `record.error`. Only running out
        of patience raises, matching `wait_for_build`.
        """
        deadline = time.monotonic() + timeout
        while True:
            record = self.get_run(run_id)
            if record.done:
                return record
            if time.monotonic() >= deadline:
                raise KubeSandboxError(f"run {run_id} still {record.status!r} after {timeout}s")
            time.sleep(poll_interval)

    # -- API keys (doc §11) ------------------------------------------------------------

    def create_api_key(self, label: str) -> CreatedApiKey:
        """Mint a service-account key for this tenant.

        The plaintext key is in the response and nowhere else — only its hash is stored,
        so it cannot be retrieved later. A key authenticates as the tenant with role
        `service`, never as the person who minted it, so it can never reach admin
        endpoints.
        """
        return CreatedApiKey.from_dict(self._json("POST", "/v1/api-keys", json={"label": label}))

    def list_api_keys(self, *, limit: int = 50, offset: int = 0) -> Page:
        """Includes revoked keys — a key silently vanishing looks identical to one that
        never existed."""
        return Page.of(
            self._json("GET", "/v1/api-keys", params={"limit": limit, "offset": offset}),
            ApiKeySummary.from_dict,
        )

    def revoke_api_key(self, key_id: str) -> None:
        """Idempotent. The row is kept, flagged revoked, so the audit trail survives."""
        t.raise_for_status(self._request("DELETE", f"/v1/api-keys/{key_id}"))

    # -- billing self-service (doc §13) ------------------------------------------------

    def billing_account(self) -> BillingAccount:
        """This tenant's mode, balance, spend cap, and month-to-date cost. Read-only —
        changing any of it is admin-only; `request_credit` is the tenant-side lever."""
        return BillingAccount.from_dict(self._json("GET", "/v1/billing/account"))

    def list_usage(self, *, since_days: int = 30, limit: int = 50, offset: int = 0) -> Page:
        """Priced usage records behind the balance, newest first. Quantities are derived
        from configured resource limits × duration, not measured consumption — billed
        amounts, not telemetry."""
        return Page.of(
            self._json(
                "GET",
                "/v1/billing/usage",
                params={"since_days": since_days, "limit": limit, "offset": offset},
            ),
            UsageRecord.from_dict,
        )

    # -- persistent workspace (doc §10.2) ---------------------------------------------

    def my_workspace(self) -> WorkspaceStatus:
        """Quota, usage, retention state. Handle all three cases: the feature may be off
        here, the caller may have no workspace yet, or there may be one."""
        return WorkspaceStatus.from_dict(self._json("GET", "/v1/workspaces/me"))
