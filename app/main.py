from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1.admin import router as admin_router
from app.api.v1.components import router as components_router
from app.api.v1.execute import router as execute_router
from app.api.v1.health import router as health_router
from app.api.v1.templates import router as templates_router
from app.core.config import get_settings
from app.core.errors import (
    ComponentNotFoundError,
    EntitlementError,
    KubeSandboxError,
    ProvisionerError,
    QuotaExceededError,
    SandboxNotFoundError,
    TemplateNotFoundError,
)
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.extensions.loader import load_registry
from app.provisioners.docker import DockerProvisioner
from app.provisioners.kubernetes import KubernetesProvisioner

logger = get_logger(__name__)

_NOT_FOUND = (ComponentNotFoundError, TemplateNotFoundError, SandboxNotFoundError)


async def _build_provisioner(settings: Settings):
    backend = settings.provisioner.backend
    if backend == "docker":
        return DockerProvisioner()
    if backend == "kubernetes":
        return await KubernetesProvisioner.create(
            kubeconfig_path=settings.provisioner.kubeconfig_path,
            namespace_prefix=settings.provisioner.namespace_prefix,
            runtime_class=settings.provisioner.runtime_class,
        )
    raise ValueError(f"unknown provisioner backend: {backend!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(debug=settings.debug)
    logger.info("startup", app_env=settings.app_env, provisioner=settings.provisioner.backend)

    app.state.registry = load_registry()
    app.state.provisioner = await _build_provisioner(settings)

    yield

    aclose = getattr(app.state.provisioner, "aclose", None)
    if aclose is not None:
        await aclose()
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
        lifespan=lifespan,
        debug=settings.debug,
    )
    _register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(execute_router)
    app.include_router(components_router)
    app.include_router(templates_router)
    app.include_router(admin_router)
    return app


app = create_app()
