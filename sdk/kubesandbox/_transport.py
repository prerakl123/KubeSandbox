"""Shared request plumbing for both clients — everything that would otherwise be
copy-pasted between the sync and async surfaces: URL joining, the auth header, error
translation, and the retry policy.

The two clients duplicate their *method surface* (that's unavoidable without hiding
one behind `asyncio.run`, which breaks inside a running event loop), but they must not
duplicate the semantics of a request. Anything a caller could observe as a behavior
difference between `KubeSandboxClient.execute()` and `AsyncKubeSandboxClient.execute()`
belongs here.
"""

from __future__ import annotations

import json as _json
from typing import Any, Mapping

import httpx

from .errors import error_for_status

DEFAULT_TIMEOUT = 180.0
"""Generous on purpose. A batch run blocks server-side until it finishes (doc §5.1),
bounded by the sandbox's own `wallClockSeconds` cap (60s by default) plus provisioning
— so a client timeout tighter than that would abandon runs the control plane is still
happily executing, and the caller would never learn the outcome."""

API_KEY_HEADER = "X-API-Key"
"""Doc §11's service-account path. The WS attach route can't use a header (browsers
can't set them on a handshake) and takes `?api_key=` instead — see `attach.py`."""


def build_headers(api_key: str | None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers[API_KEY_HEADER] = api_key
    if extra:
        headers.update(extra)
    return headers


def detail_from(response: httpx.Response) -> str:
    """The control plane answers every error as `{"detail": "..."}` (both its own
    domain-error handlers in `app/main.py` and FastAPI's `HTTPException`). A 422 from
    FastAPI's own validation nests a *list* under the same key, so that shape is
    rendered rather than str()'d into something unreadable."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or f"<empty {response.status_code} body>"
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
        return detail if isinstance(detail, str) else _json.dumps(detail)
    return _json.dumps(payload)


def raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    raise error_for_status(
        response.status_code,
        detail_from(response),
        method=response.request.method,
        url=str(response.request.url),
    )


def json_body(response: httpx.Response) -> Any:
    raise_for_status(response)
    return response.json()
