"""SecretsProvider implementations (doc §9, §20 Phase 9).

The last of the three doc §9 CloudProvider concerns to be built — `storage.py` and
`registry.py` were both pulled forward into Phase 6 because two build strategies had an
immediate need for them (see docs/TASK_CHECKLIST.md's Phase 6 section); nothing before
Phase 9 needed secrets resolution, so this one waited for its real caller rather than
being written speculatively.

Per doc §7's environment table, `local` resolves secrets from `.env`/the process
environment and never touches a cloud provider; `aks-prod` resolves them from Azure Key
Vault via the CSI driver's own workload identity (`DefaultAzureCredential`).

`DotenvSecretsProvider` is exercised live; `AzureKeyVaultSecretsProvider` is real code
(doc §9 calls Azure "implemented", not a stub) but carries the same honest "unverified
live" flag `ACRRegistryProvider`/`AzureBlobStorageProvider` already do — this machine
has no Azure credentials or vault to authenticate against. AWS/GCP are explicit stubs
using doc §9's own literal wording.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from app.cloud.base import ComingSoonProvider
from app.core.config import REPO_ROOT, SecretsSettings
from app.core.errors import SecretNotFoundError

_UNSUPPORTED_AWS = "AWS Secrets Manager support coming soon"
_UNSUPPORTED_GCP = "GCP Secret Manager support coming soon"

_ENV_PREFIX = "KUBESANDBOX_SECRET_"
"""Env-var namespace `DotenvSecretsProvider` reads. A secret named `db-password`
resolves from `KUBESANDBOX_SECRET_DB_PASSWORD` — the same `-`/`.` -> `_`, upper-cased
normalization Key Vault itself effectively forces on secret names, so a manifest or
config value referencing a secret by name works identically in both environments
without the caller knowing which backend is behind it."""


def _env_key(name: str) -> str:
    return _ENV_PREFIX + name.replace("-", "_").replace(".", "_").upper()


class SecretsProvider(Protocol):
    async def get(self, name: str) -> str:
        """Resolve a secret by name.

        Raises `SecretNotFoundError` if the configured backend has no such secret;
        any other failure (auth, network) propagates as whatever the backend client
        itself raised, so a misconfigured identity never looks like a missing secret.
        """
        ...


class DotenvSecretsProvider:
    """Real — `local`'s backend (doc §7: "`.env` / local file").

    Reads the process environment first, then falls back to parsing the repo-root
    `.env` file if one exists. Env-first ordering matters: docker-compose/`uvicorn`
    invocations inject overrides as real env vars, and those must win over a stale
    committed-adjacent `.env` (which is `.gitignore`d, but may still be lying around
    on a developer's machine from an earlier session).
    """

    def __init__(self, settings: SecretsSettings | None = None, *, dotenv_path: Path | None = None) -> None:
        self._path = dotenv_path if dotenv_path is not None else REPO_ROOT / ".env"

    def _from_dotenv(self, key: str) -> str | None:
        if not self._path.exists():
            return None
        for raw in self._path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            found, _, value = line.partition("=")
            if found.strip() == key:
                # Strip one matched layer of quotes, the only .env quoting convention
                # worth honoring here — this is a local-dev convenience reader, not a
                # full dotenv parser (no interpolation, no multiline, no `export `).
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                return value
        return None

    async def get(self, name: str) -> str:
        key = _env_key(name)
        value = os.environ.get(key)
        if value is None:
            value = self._from_dotenv(key)
        if value is None:
            raise SecretNotFoundError(
                f"secret {name!r} not found: set {key} in the environment or {self._path}"
            )
        return value


class AzureKeyVaultSecretsProvider:
    """Real — `aks-prod`'s backend (doc §7, §6 Layer 6: "secrets via Azure Key Vault").

    `DefaultAzureCredential` resolves to the pod's workload/managed identity in AKS
    (the same identity the Key Vault CSI driver mounts with), `az login` locally, or an
    env-var service principal — whichever the environment actually offers, with no
    code branch here to pick between them.

    A fresh credential + client per call rather than a cached module-level one: secret
    resolution is a startup/rare-path operation (a DSN, a signing key), not a
    per-request hot path, and holding an open credential for the process lifetime just
    to save a token exchange that the credential itself already caches internally
    isn't worth the shutdown-ordering complexity.
    """

    def __init__(self, settings: SecretsSettings) -> None:
        # Guaranteed non-None by SecretsSettings' own validator, which refuses
        # provider=azure_keyvault without a vault_url — so a misconfiguration fails
        # while parsing Settings, before anything ever constructs this.
        self._vault_url: str = settings.vault_url  # type: ignore[assignment]

    async def get(self, name: str) -> str:
        # Imported lazily so `local` (which never selects this provider) doesn't pay
        # the azure-identity/azure-keyvault import cost on every process start, and so
        # a missing optional Azure dependency can never break an unrelated import of
        # this module.
        from azure.core.exceptions import ResourceNotFoundError
        from azure.identity.aio import DefaultAzureCredential
        from azure.keyvault.secrets.aio import SecretClient

        async with DefaultAzureCredential() as credential:
            async with SecretClient(vault_url=self._vault_url, credential=credential) as client:
                try:
                    secret = await client.get_secret(name)
                except ResourceNotFoundError as exc:
                    raise SecretNotFoundError(f"secret {name!r} not found in {self._vault_url}") from exc
                if secret.value is None:
                    raise SecretNotFoundError(f"secret {name!r} exists in {self._vault_url} but has no value")
                return secret.value


class AWSSecretsProvider(ComingSoonProvider):
    coming_soon = _UNSUPPORTED_AWS

    async def get(self, name: str) -> str:
        self._raise()


class GCPSecretsProvider(ComingSoonProvider):
    coming_soon = _UNSUPPORTED_GCP

    async def get(self, name: str) -> str:
        self._raise()
