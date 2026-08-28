"""OpenTelemetry tracing (doc §14: "traces spanning API -> provisioner -> pod").

Off unless `observability.tracing_enabled` (see `ObservabilitySettings` for why the
default differs from metrics'). When on, three layers of spans stack up:

1. **Auto: inbound HTTP** — `FastAPIInstrumentor` gives one server span per request,
   already carrying route/method/status.
2. **Auto: outbound I/O** — `HTTPXClientInstrumentor` covers the Kubernetes API
   (`kubernetes_asyncio` is httpx-based) and the ACR token exchange;
   `SQLAlchemyInstrumentor` covers Postgres.
3. **Manual: the provisioner boundary** — `span()` below, used by `SandboxService` for
   acquire/exec_batch. This is the part doc §14 actually asks for and the part nothing
   auto-instruments: the hop from control plane into the data plane isn't an HTTP call
   on the Docker backend at all, and on Kubernetes it's a streaming exec that the httpx
   instrumentation would show as one long opaque request.

`aiodocker` (the `local` backend) has no OTel instrumentation of its own, so on Docker
layer 2 is empty and layer 3 is the only view into provisioning — which is precisely
why the manual spans exist rather than relying on auto-instrumentation alone.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

from app.core.config import Settings
from app.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

logger = get_logger(__name__)

_TRACER_NAME = "kubesandbox"

_configured = False
"""Guards against double-instrumentation. `configure_tracing()` is called from both the
API's lifespan and the reconciler's entrypoint, and the OTel instrumentors raise if
`instrument()` runs twice in one process — which the test suite would hit immediately,
since it builds the app more than once."""


def configure_tracing(settings: Settings, *, service_name: str | None = None) -> None:
    """Install a TracerProvider + OTLP exporter, and instrument httpx/SQLAlchemy.

    A no-op when tracing is disabled, so every caller can call it unconditionally.
    `service_name` overrides `observability.service_name` — the reconciler passes its
    own so its spans are separable from the API's in the same trace backend.
    """
    global _configured
    if not settings.observability.tracing_enabled or _configured:
        return

    # Imported inside the function, not at module scope: the OTel SDK + gRPC exporter
    # are a heavy import chain, and a deployment with tracing off (the default, and
    # every test) shouldn't pay for it just because something imported `span()`.
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    obs = settings.observability
    resource = Resource.create(
        {
            "service.name": service_name or obs.service_name,
            "service.version": "0.1.0",
            # Doc §7: exactly two environments, so this is a closed set — it's the
            # attribute a trace backend filters on to keep local spans (if anyone ever
            # points local at a collector) out of prod dashboards.
            "deployment.environment": settings.app_env,
        }
    )
    provider = TracerProvider(
        resource=resource,
        # ParentBased so a trace started by the caller (the workflow-builder, an
        # ingress) is sampled consistently end to end rather than this service
        # independently re-rolling the dice mid-trace.
        sampler=ParentBased(TraceIdRatioBased(obs.trace_sample_ratio)),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=obs.otlp_endpoint)))
    trace.set_tracer_provider(provider)

    HTTPXClientInstrumentor().instrument()
    SQLAlchemyInstrumentor().instrument(enable_commenter=False)

    _configured = True
    logger.info(
        "tracing_configured",
        otlp_endpoint=obs.otlp_endpoint,
        service_name=service_name or obs.service_name,
        sample_ratio=obs.trace_sample_ratio,
    )


def instrument_app(app: "FastAPI", settings: Settings) -> None:
    """Attach the FastAPI server-span middleware. Separate from `configure_tracing()`
    because it needs the app object, which the reconciler doesn't have."""
    if not settings.observability.tracing_enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    # /metrics and the probes would otherwise dominate span volume with zero
    # diagnostic value — every scrape and every kubelet probe becoming a trace.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="/metrics,/healthz,/readyz")


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """Manual span around a provisioner-boundary operation.

    Deliberately tolerant of tracing being off, of the OTel SDK never having been
    installed, and of the exporter failing: with no TracerProvider configured, the
    OTel API hands back a no-op tracer, so this costs one attribute lookup and a
    context-manager enter. Instrumentation must never be able to fail a real request —
    a broken collector is an observability outage, not a sandbox outage.
    """
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - opentelemetry-api is a hard dependency
        yield
        return

    tracer = trace.get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield
