"""`AsyncKubeSandboxClient` — the async mirror of `KubeSandboxClient`.

Same endpoints, same models, same exceptions; the shared semantics (auth header, error
translation, timeouts) live once in `_transport.py`. The method surface is duplicated
rather than generated or wrapped: hiding the async client behind `asyncio.run` would
break for any caller already inside an event loop (which is every caller that would
want this class), and hiding the sync one behind a thread pool would make its
tracebacks unreadable.

This is also the client the PTY attach helper composes with — see `attach.py`, since an
interactive terminal session is inherently async.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, AsyncIterator, Mapping

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


class AsyncKubeSandboxClient:
    """Asynchronous KubeSandbox control-plane client.

    ```python
    async with AsyncKubeSandboxClient("https://kubesandbox.internal", api_key="...") as ks:
        result = await ks.execute(language="python", code="print('hi')")
    ```
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = t.DEFAULT_TIMEOUT,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=t.build_headers(api_key, headers),
            transport=transport,
        )

    @property
    def api_key(self) -> str | None:
        """Read by `attach()`, which can't use the `X-API-Key` header — a WebSocket
        handshake carries the key as `?api_key=` instead (doc §5.2)."""
        return self._api_key

    # -- plumbing ---------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.request(method, path, **kwargs)

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return t.json_body(await self._request(method, path, **kwargs))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> AsyncKubeSandboxClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- health -----------------------------------------------------------------------

    async def healthz(self) -> dict:
        return await self._json("GET", "/healthz")

    async def readyz(self) -> dict:
        return await self._json("GET", "/readyz")

    # -- batch execution (doc §5.1) ---------------------------------------------------

    async def execute(
        self,
        *,
        language: str,
        code: str,
        stdin: str = "",
        version: str | None = None,
        template: str | None = None,
    ) -> BatchRunResult:
        body: dict[str, Any] = {"language": language, "code": code, "stdin": stdin}
        if version is not None:
            body["version"] = version
        if template is not None:
            body["template"] = template
        return BatchRunResult.from_dict(await self._json("POST", "/v1/execute", json=body))

    # -- sandbox lifecycle (doc §17) --------------------------------------------------

    async def create_sandbox(
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
        return Sandbox.from_dict(await self._json("POST", "/v1/sandboxes", json=body))

    async def get_sandbox(self, sandbox_id: str) -> Sandbox:
        return Sandbox.from_dict(await self._json("GET", f"/v1/sandboxes/{sandbox_id}"))

    async def destroy_sandbox(self, sandbox_id: str) -> None:
        t.raise_for_status(await self._request("DELETE", f"/v1/sandboxes/{sandbox_id}"))

    async def run(
        self, sandbox_id: str, *, code: str, stdin: str = "", language: str | None = None
    ) -> BatchRunResult:
        body: dict[str, Any] = {"code": code, "stdin": stdin}
        if language is not None:
            body["language"] = language
        return BatchRunResult.from_dict(
            await self._json("POST", f"/v1/sandboxes/{sandbox_id}/runs", json=body)
        )

    @asynccontextmanager
    async def sandbox(
        self,
        *,
        language: str,
        version: str | None = None,
        template: str | None = None,
        persistent: bool = False,
    ) -> AsyncIterator[Sandbox]:
        """Create, hand over, always destroy — see `KubeSandboxClient.sandbox`."""
        created = await self.create_sandbox(
            language=language, version=version, template=template, persistent=persistent
        )
        try:
            yield created
        finally:
            await self.destroy_sandbox(created.id)

    # -- workspace files (doc §5.4) ---------------------------------------------------

    async def upload_file(self, sandbox_id: str, path: str, content: str) -> None:
        t.raise_for_status(
            await self._request(
                "PUT",
                f"/v1/sandboxes/{sandbox_id}/files",
                params={"path": path},
                content=content.encode("utf-8"),
                headers={"Content-Type": "application/octet-stream"},
            )
        )

    async def download_file(self, sandbox_id: str, path: str) -> bytes:
        response = await self._request("GET", f"/v1/sandboxes/{sandbox_id}/files", params={"path": path})
        t.raise_for_status(response)
        return response.content

    async def list_tree(self, sandbox_id: str, path: str = "") -> list[FileEntry]:
        payload = await self._json("GET", f"/v1/sandboxes/{sandbox_id}/tree", params={"path": path})
        return [FileEntry.from_dict(entry) for entry in payload]

    # -- catalog (doc §3, entitlement-filtered per caller) ----------------------------

    async def list_components(self, *, category: str | None = None) -> list[Component]:
        params = {"category": category} if category else None
        payload = await self._json("GET", "/v1/components", params=params)
        return [Component.from_dict(c) for c in payload]

    async def get_component_versions(self, name: str) -> dict:
        return await self._json("GET", f"/v1/components/{name}")

    async def list_templates(self) -> list[Template]:
        return [Template.from_dict(x) for x in await self._json("GET", "/v1/templates")]

    # -- builds (doc §8) --------------------------------------------------------------

    async def trigger_build(self, name: str, *, version: str | None = None) -> Build:
        params = {"version": version} if version else None
        return Build.from_dict(await self._json("POST", f"/v1/components/{name}/build", params=params))

    async def get_build(self, build_id: str) -> Build:
        return Build.from_dict(await self._json("GET", f"/v1/builds/{build_id}"))

    async def wait_for_build(
        self, build_id: str, *, timeout: float = 1800.0, poll_interval: float = 5.0
    ) -> Build:
        deadline = time.monotonic() + timeout
        while True:
            build = await self.get_build(build_id)
            if build.done:
                return build
            if time.monotonic() >= deadline:
                raise KubeSandboxError(f"build {build_id} still {build.status!r} after {timeout}s")
            await asyncio.sleep(poll_interval)

    # -- billing self-service (doc §13) -----------------------------------------------

    async def request_credit(self, *, amount: float, reason: str) -> CreditRequest:
        return CreditRequest.from_dict(
            await self._json(
                "POST", "/v1/billing/credit-requests", json={"amount": amount, "reason": reason}
            )
        )

    async def list_credit_requests(self) -> list[CreditRequest]:
        payload = await self._json("GET", "/v1/billing/credit-requests")
        return [CreditRequest.from_dict(x) for x in payload]

    # -- identity (Phase 9) -----------------------------------------------------------

    async def auth_config(self) -> dict:
        """Public OIDC configuration for a frontend. The one endpoint that needs no
        credential — it's what a client calls before it has one."""
        return await self._json("GET", "/v1/auth/config")

    async def exchange_oidc_token(self, oidc_token: str) -> dict:
        """Trade an IdP token for a KubeSandbox session token (doc §11).

        Returns the raw payload (`access_token`, `expires_in`, `principal`) rather than a
        model, because the useful next step is constructing a new client with the token —
        this SDK deliberately does not mutate its own auth in place, since a client whose
        credential changes under concurrent use is a debugging nightmare.
        """
        return await self._json("POST", "/v1/auth/token", json={"oidc_token": oidc_token})

    async def me(self) -> Identity:
        """Who am I, and which optional features are enabled here. Worth calling once
        after authenticating: `identity.features` tells you whether persistence, billing,
        or pooling are on, and calling into a disabled one returns a 400."""
        return Identity.from_dict(await self._json("GET", "/v1/me"))

    # -- listings (Phase 9) -----------------------------------------------------------

    async def list_sandboxes(
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
        return Page.of(await self._json("GET", "/v1/sandboxes", params=params), Sandbox.from_dict)

    async def list_runs(
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
        return Page.of(await self._json("GET", "/v1/runs", params=params), RunRecord.from_dict)

    async def get_run(self, run_id: str) -> RunRecord:
        """One run in full, including its bundled result once terminal (doc §5.1)."""
        return RunRecord.from_dict(await self._json("GET", f"/v1/runs/{run_id}"))

    async def list_builds(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> Page:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status is not None:
            params["status"] = status
        return Page.of(await self._json("GET", "/v1/builds", params=params), Build.from_dict)

    async def get_template(self, name: str) -> dict:
        """All visible versions of one template with their full resource/TTL shape.
        Returned raw: the detail payload is a `{name, versions}` wrapper whose versions
        carry more fields than `Template` models, and flattening it would lose them."""
        return await self._json("GET", f"/v1/templates/{name}")

    # -- async execution (doc §5.1's ?async=true) --------------------------------------

    async def execute_async(
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
        # Parenthesized: `await x(...)["k"]` subscripts the coroutine, not its result.
        payload = await self._json("POST", "/v1/execute", params={"async": True}, json=body)
        return payload["run_id"]

    async def wait_for_run(
        self, run_id: str, *, timeout: float = 300.0, poll_interval: float = 1.0
    ) -> RunRecord:
        """Poll until the run is terminal, or raise on timeout.

        A *failed* run is returned, not raised — inspect `record.error`. Only running out
        of patience raises, matching `wait_for_build`.
        """
        deadline = time.monotonic() + timeout
        while True:
            record = await self.get_run(run_id)
            if record.done:
                return record
            if time.monotonic() >= deadline:
                raise KubeSandboxError(f"run {run_id} still {record.status!r} after {timeout}s")
            await asyncio.sleep(poll_interval)

    # -- API keys (doc §11) ------------------------------------------------------------

    async def create_api_key(self, label: str) -> CreatedApiKey:
        """Mint a service-account key for this tenant.

        The plaintext key is in the response and nowhere else — only its hash is stored,
        so it cannot be retrieved later. A key authenticates as the tenant with role
        `service`, never as the person who minted it, so it can never reach admin
        endpoints.
        """
        return CreatedApiKey.from_dict(await self._json("POST", "/v1/api-keys", json={"label": label}))

    async def list_api_keys(self, *, limit: int = 50, offset: int = 0) -> Page:
        """Includes revoked keys — a key silently vanishing looks identical to one that
        never existed."""
        return Page.of(
            await self._json("GET", "/v1/api-keys", params={"limit": limit, "offset": offset}),
            ApiKeySummary.from_dict,
        )

    async def revoke_api_key(self, key_id: str) -> None:
        """Idempotent. The row is kept, flagged revoked, so the audit trail survives."""
        t.raise_for_status(await self._request("DELETE", f"/v1/api-keys/{key_id}"))

    # -- billing self-service (doc §13) ------------------------------------------------

    async def billing_account(self) -> BillingAccount:
        """This tenant's mode, balance, spend cap, and month-to-date cost. Read-only —
        changing any of it is admin-only; `request_credit` is the tenant-side lever."""
        return BillingAccount.from_dict(await self._json("GET", "/v1/billing/account"))

    async def list_usage(self, *, since_days: int = 30, limit: int = 50, offset: int = 0) -> Page:
        """Priced usage records behind the balance, newest first. Quantities are derived
        from configured resource limits × duration, not measured consumption — billed
        amounts, not telemetry."""
        return Page.of(
            await self._json(
                "GET",
                "/v1/billing/usage",
                params={"since_days": since_days, "limit": limit, "offset": offset},
            ),
            UsageRecord.from_dict,
        )

    # -- persistent workspace (doc §10.2) ---------------------------------------------

    async def my_workspace(self) -> WorkspaceStatus:
        """Quota, usage, retention state. Handle all three cases: the feature may be off
        here, the caller may have no workspace yet, or there may be one."""
        return WorkspaceStatus.from_dict(await self._json("GET", "/v1/workspaces/me"))
