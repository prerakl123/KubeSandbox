"""SDK exception hierarchy — a client-side mirror of the control plane's own
`app/core/errors.py` mapping (doc §17's status codes), so a caller catches a named
exception rather than inspecting `response.status_code`.

Deliberately *not* importing anything from `app/` — the SDK is an independently
installable package (see `sdk/pyproject.toml`) that a workflow-builder pip-installs
without the control plane's fastapi/sqlalchemy/kubernetes/azure dependency tree.
"""

from __future__ import annotations


class KubeSandboxError(Exception):
    """Base for every error this SDK raises."""


class KubeSandboxAPIError(KubeSandboxError):
    """A non-2xx response from the control plane.

    `detail` is the server's own `{"detail": ...}` message when it sent one — every
    handler in `app/main.py` and every `HTTPException` in the routers uses that shape —
    falling back to the raw body otherwise.
    """

    def __init__(self, status_code: int, detail: str, *, method: str = "", url: str = "") -> None:
        where = f" ({method} {url})" if method and url else ""
        super().__init__(f"HTTP {status_code}: {detail}{where}")
        self.status_code = status_code
        self.detail = detail
        self.method = method
        self.url = url


class BadRequestError(KubeSandboxAPIError):
    """400/422 — a malformed request, or a domain error the control plane maps to 400
    (`KubeSandboxError`'s generic handler: an unknown language, a template/component
    mismatch, persistence not enabled in that environment)."""


class AuthenticationError(KubeSandboxAPIError):
    """401 — missing, invalid, or revoked API key (doc §11)."""


class PermissionDeniedError(KubeSandboxAPIError):
    """403 — an entitlement failure (doc §3.6) or an admin-only endpoint reached with a
    non-admin principal."""


class NotFoundError(KubeSandboxAPIError):
    """404 — no such sandbox/component/template/build. Note that a sandbox belonging to
    a *different* tenant also reports 404, never 403, by design (see
    `SandboxService.get_sandbox`) — so this never confirms another tenant's ids exist."""


class ConflictError(KubeSandboxAPIError):
    """409 — a second concurrent viewer tried to attach to a sandbox that already has
    one (doc §5.2: single-viewer, no multiplexing in v1)."""


class QuotaExceededError(KubeSandboxAPIError):
    """429 — a quota, or billing pre-authorization (insufficient credit / spend cap
    exceeded, doc §13). `POST /v1/billing/credit-requests` is the self-service path
    forward; see `KubeSandboxClient.request_credit`."""


class ProvisionerError(KubeSandboxAPIError):
    """502 — the Docker/Kubernetes backend failed to satisfy the request. Retryable in
    principle (a node under pressure, an image pull failure), unlike a 4xx."""


class ServiceUnavailableError(KubeSandboxAPIError):
    """503 — the replica reported itself not ready (`GET /readyz`), i.e. Postgres or
    Redis is unreachable from it."""


_STATUS_MAP: dict[int, type[KubeSandboxAPIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: BadRequestError,
    429: QuotaExceededError,
    502: ProvisionerError,
    503: ServiceUnavailableError,
}


def error_for_status(status_code: int, detail: str, *, method: str = "", url: str = "") -> KubeSandboxAPIError:
    """Map a status code onto the most specific exception class available, falling back
    to the generic `KubeSandboxAPIError` for anything unmapped (a 500, a proxy's 504) —
    an unmapped code must still raise, never be swallowed."""
    cls = _STATUS_MAP.get(status_code, KubeSandboxAPIError)
    return cls(status_code, detail, method=method, url=url)
