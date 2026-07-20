"""ImageRegistryProvider implementations (doc §9, §20 Phase 6).

`LocalImageStore` is the only one exercised against a live backend this phase — the
`registry:2` container in docker-compose.yml has been running since Phase 1, defined
but never pushed to or pulled from until now (doc's own Phase 6 checklist wording).

`ACRRegistryProvider` is a real implementation (doc §9 calls Azure "implemented", not
a stub) via the standard ACR OAuth2 token-exchange flow: an AAD access token
(`DefaultAzureCredential`, audience `https://containerregistry.azure.net` — confirmed
against Microsoft's own `ContainerRegistryClient` default audience, Microsoft Learn)
is exchanged for an ACR refresh token via `POST <endpoint>/oauth2/exchange`, which is
then used as the password half of a Docker Registry v2 basic-auth push — this is the
same flow `docker login`/`az acr login` perform under the hood, just without shelling
out to the `az` CLI. Structurally correct, but **not exercised live this session** —
no Azure credentials/environment available — the same honest "real code, unverified
live" flag Phase 3 already carries for untested gVisor.
"""

from __future__ import annotations

from typing import Protocol

import aiodocker
import httpx
from azure.identity.aio import DefaultAzureCredential

from app.core.config import ImageRegistrySettings
from app.core.errors import BuildError

_ACR_TOKEN_AUDIENCE = "https://containerregistry.azure.net/.default"
_ANONYMOUS_SENTINEL = "00000000-0000-0000-0000-000000000000"
"""ACR's documented sentinel username for token-based (as opposed to admin-user)
basic auth — the password is the exchanged ACR refresh token, never a real password."""

_UNSUPPORTED = "ECR/Artifact Registry support coming soon"


def _split_repo_tag(local_tag: str) -> tuple[str, str]:
    repository, sep, tag = local_tag.rpartition(":")
    if not sep:
        raise BuildError(f"image ref {local_tag!r} has no tag")
    return repository, tag


async def _tag_and_push(local_tag: str, remote_repo: str, tag: str, *, auth: dict | None = None) -> None:
    """Shared by LocalImageStore and ACRRegistryProvider — same aiodocker retag+push
    sequence against the local daemon, differing only in the auth header."""
    docker = aiodocker.Docker()
    try:
        await docker.images.tag(local_tag, remote_repo, tag=tag)
        await docker.images.push(remote_repo, tag=tag, auth=auth)
    finally:
        await docker.close()


class ImageRegistryProvider(Protocol):
    async def push(self, local_tag: str) -> str:
        """Push an already-built local image (e.g. "kubesandbox/jq:1.0") and return
        the resolved, pullable ref (e.g. "localhost:5000/kubesandbox/jq:1.0")."""
        ...

    async def resolve(self, ref: str) -> str:
        """The fully-qualified pull ref for a bare "repo:tag", without pushing."""
        ...


class LocalImageStore:
    """Real — retags + pushes to the local pull-based registry:2 (doc §8.1's
    ACR-shaped local stand-in), via aiodocker against the same local daemon
    DockerfileBuildStrategy just built the image on."""

    def __init__(self, settings: ImageRegistrySettings) -> None:
        self._endpoint = settings.endpoint  # e.g. "localhost:5000"

    async def push(self, local_tag: str) -> str:
        repository, tag = _split_repo_tag(local_tag)
        remote_repo = f"{self._endpoint}/{repository}"
        await _tag_and_push(local_tag, remote_repo, tag)
        return f"{remote_repo}:{tag}"

    async def resolve(self, ref: str) -> str:
        return f"{self._endpoint}/{ref}"


class ACRRegistryProvider:
    def __init__(self, settings: ImageRegistrySettings) -> None:
        self._endpoint = settings.endpoint  # e.g. "kubesandboxprod.azurecr.io"

    async def _acr_refresh_token(self) -> str:
        async with DefaultAzureCredential() as credential:
            aad_token = await credential.get_token(_ACR_TOKEN_AUDIENCE)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://{self._endpoint}/oauth2/exchange",
                data={
                    "grant_type": "access_token",
                    "service": self._endpoint,
                    "access_token": aad_token.token,
                },
            )
            response.raise_for_status()
            return response.json()["refresh_token"]

    async def push(self, local_tag: str) -> str:
        repository, tag = _split_repo_tag(local_tag)
        remote_repo = f"{self._endpoint}/{repository}"
        refresh_token = await self._acr_refresh_token()
        auth = {
            "username": _ANONYMOUS_SENTINEL,
            "password": refresh_token,
            "serveraddress": self._endpoint,
        }
        await _tag_and_push(local_tag, remote_repo, tag, auth=auth)
        return f"{remote_repo}:{tag}"

    async def resolve(self, ref: str) -> str:
        return f"{self._endpoint}/{ref}"


class AWSImageRegistryProvider:
    async def push(self, local_tag: str) -> str:
        raise NotImplementedError(_UNSUPPORTED)

    async def resolve(self, ref: str) -> str:
        raise NotImplementedError(_UNSUPPORTED)


class GCPImageRegistryProvider:
    async def push(self, local_tag: str) -> str:
        raise NotImplementedError(_UNSUPPORTED)

    async def resolve(self, ref: str) -> str:
        raise NotImplementedError(_UNSUPPORTED)
