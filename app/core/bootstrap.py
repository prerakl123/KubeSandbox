"""Shared construction of environment-selected backends (doc §7, §9) — factored out of
`app/main.py`'s `lifespan` so the Phase 7 reconciler (a separate worker process, doc
§4.1) can build the exact same `Provisioner`/`ObjectStorageProvider`/`ImageRegistryProvider`/
`SecretsProvider` without duplicating the `if provider == ...` dispatch in two places
and risking them drifting apart.

`validate_cloud_providers()` (Phase 9) is the startup half of doc §9's "stubs fail
loudly and immediately ... caught at startup/config-validation time, not mid-request"
contract: the factories below happily *construct* an AWS/GCP stub (they have to, for
the check to have something to inspect), and this is where selecting one becomes a
hard startup failure naming the offending setting.
"""

from __future__ import annotations

from app.cloud.base import assert_cloud_provider_usable
from app.cloud.registry import (
    ACRRegistryProvider,
    AWSImageRegistryProvider,
    GCPImageRegistryProvider,
    ImageRegistryProvider,
    LocalImageStore,
)
from app.cloud.secrets import (
    AWSSecretsProvider,
    AzureKeyVaultSecretsProvider,
    DotenvSecretsProvider,
    GCPSecretsProvider,
    SecretsProvider,
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
        return DockerProvisioner(
            seccomp_profile=settings.provisioner.seccomp_profile,
            apparmor_profile=settings.provisioner.apparmor_profile,
        )
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


def build_secrets_provider(settings: Settings) -> SecretsProvider:
    provider = settings.secrets.provider
    if provider == "dotenv":
        return DotenvSecretsProvider(settings.secrets)
    if provider == "azure_keyvault":
        return AzureKeyVaultSecretsProvider(settings.secrets)
    if provider == "aws":
        return AWSSecretsProvider()
    if provider == "gcp":
        return GCPSecretsProvider()
    raise ValueError(f"unknown secrets provider: {provider!r}")


def validate_cloud_providers(settings: Settings) -> None:
    """Fail fast at startup if any doc §9 concern selects an unimplemented cloud.

    Checks all three concerns before raising on any one of them would be nicer for a
    doubly-misconfigured deployment, but `assert_cloud_provider_usable` raising on the
    first is what makes the message unambiguous about *which* setting to fix; a second
    startup attempt surfaces the next one.
    """
    assert_cloud_provider_usable(
        build_secrets_provider(settings),
        concern="secrets",
        configured=settings.secrets.provider,
    )
    assert_cloud_provider_usable(
        build_object_storage_provider(settings),
        concern="object_storage",
        configured=settings.object_storage.provider,
    )
    assert_cloud_provider_usable(
        build_image_registry_provider(settings),
        concern="image_registry",
        configured=settings.image_registry.provider,
    )
