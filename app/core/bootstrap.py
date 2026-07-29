"""Shared construction of environment-selected backends (doc §7, §9) — factored out of
`app/main.py`'s `lifespan` so the Phase 7 reconciler (a separate worker process, doc
§4.1) can build the exact same `Provisioner`/`ObjectStorageProvider`/`ImageRegistryProvider`
without duplicating the `if provider == ...` dispatch in two places and risking them
drifting apart.
"""

from __future__ import annotations

from app.cloud.registry import (
    ACRRegistryProvider,
    AWSImageRegistryProvider,
    GCPImageRegistryProvider,
    ImageRegistryProvider,
    LocalImageStore,
)
from app.cloud.storage import (
    AWSObjectStorageProvider,
    AzureBlobStorageProvider,
    GCPObjectStorageProvider,
    MinIOStorageProvider,
    ObjectStorageProvider,
)
from app.core.config import Settings
from app.provisioners.docker import DockerProvisioner
from app.provisioners.kubernetes import KubernetesProvisioner


async def build_provisioner(settings: Settings):
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


def build_image_registry_provider(settings: Settings) -> ImageRegistryProvider:
    provider = settings.image_registry.provider
    if provider == "local":
        return LocalImageStore(settings.image_registry)
    if provider == "acr":
        return ACRRegistryProvider(settings.image_registry)
    if provider == "aws":
        return AWSImageRegistryProvider()
    if provider == "gcp":
        return GCPImageRegistryProvider()
    raise ValueError(f"unknown image_registry provider: {provider!r}")


def build_object_storage_provider(settings: Settings) -> ObjectStorageProvider:
    provider = settings.object_storage.provider
    if provider == "minio":
        return MinIOStorageProvider(settings.object_storage)
    if provider == "azure_blob":
        return AzureBlobStorageProvider(settings.object_storage)
    if provider == "aws":
        return AWSObjectStorageProvider()
    if provider == "gcp":
        return GCPObjectStorageProvider()
    raise ValueError(f"unknown object_storage provider: {provider!r}")
