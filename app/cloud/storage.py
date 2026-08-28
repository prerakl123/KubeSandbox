"""ObjectStorageProvider implementations (doc §9).

Pulled forward from its natural home (roadmap Phase 9) because two Phase 6 build
strategies have a genuine, immediate need for it: PipelineBuildStrategy's step cache
and HelmChartStrategy's rendered-manifest artifact storage (see
docs/TASK_CHECKLIST.md's Phase 6 section) — not built speculatively ahead of a real
caller. SecretsProvider/ImageRegistryProvider are separate cloud/ concerns —
`registry.py` came along in the same phase for the same reason; `secrets.py` had no
caller until Phase 9 and was built then (see its own module docstring).

`MinIOStorageProvider` is the only one of these actually exercised against a live
backend this phase — the `minio` service in docker-compose.yml has been running since
Phase 1, just unused until now. `AzureBlobStorageProvider` is a real implementation
(doc §9 calls Azure "implemented", not a stub), but this session has no Azure
credentials/environment to exercise it against — same honest "real code, unverified
live" flag Phase 3 already carries for gVisor. AWS/GCP are explicit stubs per doc §9,
using its own literal wording, so a misconfiguration fails loudly at startup rather
than silently no-op'ing mid-build.
"""

from __future__ import annotations

from typing import Protocol

import aioboto3
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient
from botocore.exceptions import ClientError

from app.cloud.base import ComingSoonProvider
from app.core.config import ObjectStorageSettings

_UNSUPPORTED = "S3/GCS support coming soon"


class ObjectStorageProvider(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
    """Raises KeyError if `key` doesn't exist — callers (PipelineBuildStrategy's cache
    check, HelmChartStrategy) use this to distinguish "not cached yet" from a real
    transport error, which propagates as whatever the backend client itself raises."""
    async def delete(self, key: str) -> None: ...
    """Permanently removes an object — doc §10.2's hard-delete step for a workspace
    past its archive grace period (Phase 7). Idempotent: deleting an already-gone key
    is success, not an error, matching every other teardown-style call in this
    codebase (Provisioner.destroy(), etc.)."""


class MinIOStorageProvider:
    """S3-compatible (doc §9: "doubles as the local stand-in for S3 later") — built on
    aioboto3's S3 client, the same client a future real AWS implementation would reuse,
    just pointed at MinIO's endpoint with path-style addressing."""

    def __init__(self, settings: ObjectStorageSettings) -> None:
        self._endpoint = settings.endpoint
        self._access_key = settings.access_key
        self._secret_key = settings.secret_key
        self._bucket = settings.bucket
        self._session = aioboto3.Session()

    def _client(self):
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )

    async def _ensure_bucket(self, client) -> None:
        try:
            await client.head_bucket(Bucket=self._bucket)
        except ClientError:
            await client.create_bucket(Bucket=self._bucket)

    async def put(self, key: str, data: bytes) -> None:
        async with self._client() as client:
            await self._ensure_bucket(client)
            await client.put_object(Bucket=self._bucket, Key=key, Body=data)

    async def get(self, key: str) -> bytes:
        async with self._client() as client:
            # The bucket may not exist yet at all — e.g. the very first cache lookup
            # a PipelineBuildStrategy ever makes, before anything has been put() —
            # confirmed live: get_object against a missing bucket raises NoSuchBucket,
            # not NoSuchKey, so without this the caller's "not found -> KeyError"
            # contract (used to mean "cache miss") never actually triggers; it's
            # semantically the same as "key not found" either way.
            await self._ensure_bucket(client)
            try:
                response = await client.get_object(Bucket=self._bucket, Key=key)
            except client.exceptions.NoSuchKey as exc:
                raise KeyError(key) from exc
            async with response["Body"] as stream:
                return await stream.read()

    async def delete(self, key: str) -> None:
        # S3's DELETE is idempotent by design (204 whether or not the key existed) —
        # no NoSuchKey to catch here, unlike get().
        async with self._client() as client:
            await self._ensure_bucket(client)
            await client.delete_object(Bucket=self._bucket, Key=key)


class AzureBlobStorageProvider:
    """Real Azure Blob implementation via the async `azure-storage-blob` client,
    authenticated through `DefaultAzureCredential` (managed identity in AKS, `az
    login` locally, or env-var service principal — whichever the environment offers).
    Fails loudly if no credential is available, matching doc §9's cloud-stub
    philosophy even though this isn't a stub."""

    def __init__(self, settings: ObjectStorageSettings) -> None:
        self._account_url = settings.endpoint
        self._container = settings.bucket

    async def put(self, key: str, data: bytes) -> None:
        async with DefaultAzureCredential() as credential:
            async with BlobServiceClient(self._account_url, credential=credential) as service:
                container = service.get_container_client(self._container)
                await container.upload_blob(name=key, data=data, overwrite=True)

    async def get(self, key: str) -> bytes:
        async with DefaultAzureCredential() as credential:
            async with BlobServiceClient(self._account_url, credential=credential) as service:
                blob = service.get_container_client(self._container).get_blob_client(key)
                if not await blob.exists():
                    raise KeyError(key)
                downloader = await blob.download_blob()
                return await downloader.readall()

    async def delete(self, key: str) -> None:
        async with DefaultAzureCredential() as credential:
            async with BlobServiceClient(self._account_url, credential=credential) as service:
                blob = service.get_container_client(self._container).get_blob_client(key)
                # delete_blob() itself 404s on an already-gone blob — swallow that one
                # case to match the idempotent-teardown contract every other delete-
                # style call in this codebase already follows.
                if await blob.exists():
                    await blob.delete_blob()


class AWSObjectStorageProvider(ComingSoonProvider):
    coming_soon = _UNSUPPORTED

    async def put(self, key: str, data: bytes) -> None:
        self._raise()

    async def get(self, key: str) -> bytes:
        self._raise()

    async def delete(self, key: str) -> None:
        self._raise()


class GCPObjectStorageProvider(ComingSoonProvider):
    coming_soon = _UNSUPPORTED

    async def put(self, key: str, data: bytes) -> None:
        self._raise()

    async def get(self, key: str) -> bytes:
        self._raise()

    async def delete(self, key: str) -> None:
        self._raise()
