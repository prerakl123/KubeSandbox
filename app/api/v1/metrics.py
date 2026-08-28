"""`GET /metrics` — Prometheus scrape endpoint (doc §17's own endpoint list, §14).

Unversioned like `/healthz`/`/readyz`, for the same reason: it's an operational
endpoint for the scraper, not part of the `/v1` consumer API contract. Registered by
`app/main.py` only when `observability.metrics_enabled`, so a deployment that turns
metrics off has no endpoint at all rather than one returning an empty body.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["Observability"])


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    description=(
        "Text-format exposition of this replica's in-process metrics (doc §14). "
        "Deliberately unauthenticated: it's scraped by Prometheus inside the cluster, "
        "and the Helm chart's Service exposes it on the pod network only — never "
        "through the ingress. Tenant ids appear as label values on "
        "`kubesandbox_credit_balance`, so treat it as internal, not public."
    ),
    response_class=Response,
    responses={200: {"content": {CONTENT_TYPE_LATEST: {}}}},
)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
