from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1.admin import router as admin_router
from app.api.v1.builds import router as builds_router
from app.api.v1.components import router as components_router
from app.api.v1.execute import router as execute_router
from app.api.v1.health import router as health_router
from app.api.v1.sandboxes import router as sandboxes_router
from app.api.v1.templates import router as templates_router
from app.core.bootstrap import build_image_registry_provider, build_object_storage_provider, build_provisioner
from app.core.config import get_settings
from app.core.errors import (
    BuildNotFoundError,
    ComponentNotFoundError,
    EntitlementError,
    KubeSandboxError,
    ProvisionerError,
    QuotaExceededError,
    SandboxNotFoundError,
    TemplateNotFoundError,
)
from app.core.logging import configure_logging, get_logger
from app.extensions.loader import load_registry
from app.persistence.db import get_session_factory
from app.persistence.redis import build_redis_client
from app.services.build_manager import BuildManager
from app.services.entitlement_service import EntitlementService
from app.streaming.ws_gateway import router as ws_router

logger = get_logger(__name__)

_NOT_FOUND = (ComponentNotFoundError, TemplateNotFoundError, SandboxNotFoundError, BuildNotFoundError)

_API_DESCRIPTION = """
A configurable, plug-and-play control plane that provisions isolated, per-request
sandbox environments on Kubernetes (`aks-prod`) or Docker (`local`), streams their I/O
to consumers, and exposes languages, databases, and tools as declaratively defined,
versioned extensions.

Two consumers, two execution modes (see `docs/ARCHITECTURE_AND_PLAN.md` §1, §5):

* **Workflow-builder "code block"** — batch: `POST /v1/execute` or
  `POST /v1/sandboxes/{id}/runs`, stdin supplied up front, one bundled result back.
* **Standalone users** — interactive: `WS /v1/sandboxes/{id}/attach`, a live PTY
  (not shown below — WebSocket routes aren't representable in OpenAPI/Swagger).
"""

_TAGS_METADATA = [
    {
        "name": "Health",
        "description": "Liveness/readiness probes for the control plane process itself.",
    },
    {
        "name": "Execution",
        "description": (
            "One-shot ephemeral batch execution — acquire, run to completion, tear "
            "down. The workflow-builder 'code block' convenience path (doc §5.1)."
        ),
    },
    {
        "name": "Sandboxes",
        "description": (
            "Non-ephemeral sandbox lifecycle: create, inspect status, run further "
            "batch commands against, upload/download workspace files, and destroy "
            "(doc §17). Backs interactive PTY attach, which lives on its own "
            "WebSocket route below."
        ),
    },
    {
        "name": "Streaming",
        "description": (
            "Interactive PTY attach for standalone human users (doc §5.2) — "
            "`WS /v1/sandboxes/{id}/attach`. Not documented below: WebSocket routes "
            "have no representation in the OpenAPI/Swagger spec."
        ),
    },
    {
        "name": "Components",
        "description": (
            "The component registry — versioned language/database/tool/service "
            "manifests, entitlement-filtered per caller (doc §3)."
        ),
    },
    {
        "name": "Templates",
        "description": "SandboxTemplate blueprints — base + composed components (doc §3.4).",
    },
    {
        "name": "Admin",
        "description": (
            "Catalog curation: who may see or publish which components/templates "
            "(doc §3.6). Admin-role only."
        ),
    },
    {
        "name": "Builds",
        "description": (
            "The build system (doc §8) — turns a component's declared build strategy "
            "(dockerfile/compose/pipeline/helm) into a real, pushed golden image via "
            "BuildManager. Runs in the background; poll GET /v1/builds/{id}."
        ),
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(debug=settings.debug)
    logger.info("startup", app_env=settings.app_env, provisioner=settings.provisioner.backend)

    app.state.registry = load_registry()
    app.state.provisioner = await build_provisioner(settings)
    app.state.redis = build_redis_client(settings)
    app.state.image_registry_provider = build_image_registry_provider(settings)
    app.state.object_storage_provider = build_object_storage_provider(settings)

    # Rehydrate Registry.built_images from the latest successful Build row per
    # component (doc §8, Phase 6) — otherwise a control-plane restart would forget
    # every previously-built non-"image"-sourced component until it's rebuilt.
    async with get_session_factory()() as session:
        build_manager = BuildManager(
            app.state.registry,
            EntitlementService(session),
            app.state.image_registry_provider,
            app.state.object_storage_provider,
            get_session_factory(),
        )
        await build_manager.hydrate_built_images(session)

    yield

    aclose = getattr(app.state.provisioner, "aclose", None)
    if aclose is not None:
        await aclose()
    await app.state.redis.aclose()
    logger.info("shutdown")


def _register_exception_handlers(app: FastAPI) -> None:
    # Registered per-class (not as a tuple) since Starlette's handler resolution keys
    # on the exact exception type before walking the MRO — a tuple key would never match.
    async def _not_found(request: Request, exc: KubeSandboxError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    async def _forbidden(request: Request, exc: EntitlementError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    async def _quota(request: Request, exc: QuotaExceededError) -> JSONResponse:
        return JSONResponse(status_code=429, content={"detail": str(exc)})

    async def _provisioner_error(request: Request, exc: ProvisionerError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    async def _generic(request: Request, exc: KubeSandboxError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    for exc_cls in _NOT_FOUND:
        app.add_exception_handler(exc_cls, _not_found)
    app.add_exception_handler(EntitlementError, _forbidden)
    app.add_exception_handler(QuotaExceededError, _quota)
    app.add_exception_handler(ProvisionerError, _provisioner_error)
    app.add_exception_handler(KubeSandboxError, _generic)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="KubeSandbox",
        version="0.1.0",
        description=_API_DESCRIPTION,
        lifespan=lifespan,
        debug=settings.debug,
        openapi_tags=_TAGS_METADATA,
        # Collapses the auto-generated "Schemas" section at the bottom of Swagger UI —
        # every field a caller needs is already documented inline on its own
        # request/response model and parameter, so the raw schema dump is just noise.
        swagger_ui_parameters={"defaultModelsExpandDepth": -1},
    )
    _register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(execute_router)
    app.include_router(sandboxes_router)
    app.include_router(ws_router)
    app.include_router(components_router)
    app.include_router(templates_router)
    app.include_router(admin_router)
    app.include_router(builds_router)
    return app


app = create_app()
