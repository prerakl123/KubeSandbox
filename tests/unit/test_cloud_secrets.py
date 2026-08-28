"""Tests for `app/cloud/secrets.py` and the doc §9 fail-fast contract (Phase 9).

The fail-fast half is the part worth testing hardest. Doc §9 requires that selecting an
unimplemented cloud is "caught at startup/config-validation time, not mid-request", and
before Phase 9 nothing in the test suite touched any of the stubs at all — they had
never been proven to raise, which is the only thing that made them stubs rather than
silent no-ops.

`AzureKeyVaultSecretsProvider` is not exercised against a live vault (no Azure
credentials here), matching the standing flag on `ACRRegistryProvider` and
`AzureBlobStorageProvider`. What *is* tested is that constructing it doesn't require
Azure, so the module is importable and the `local` profile never pays for it.
"""

from __future__ import annotations

import pytest

from app.cloud.base import ComingSoonProvider, assert_cloud_provider_usable
from app.cloud.registry import AWSImageRegistryProvider, GCPImageRegistryProvider, LocalImageStore
from app.cloud.secrets import (
    AWSSecretsProvider,
    AzureKeyVaultSecretsProvider,
    DotenvSecretsProvider,
    GCPSecretsProvider,
    _env_key,
)
from app.cloud.storage import AWSObjectStorageProvider, GCPObjectStorageProvider
from app.core.bootstrap import (
    build_image_registry_provider,
    build_object_storage_provider,
    build_secrets_provider,
    validate_cloud_providers,
)
from app.core.config import Settings
from app.core.errors import ConfigurationError, SecretNotFoundError


# -- DotenvSecretsProvider ------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("db-password", "KUBESANDBOX_SECRET_DB_PASSWORD"),
        ("db.password", "KUBESANDBOX_SECRET_DB_PASSWORD"),
        ("jwt_secret", "KUBESANDBOX_SECRET_JWT_SECRET"),
    ],
)
def test_env_key_normalization_matches_keyvault_naming(name, expected) -> None:
    """The same `-`/`.` -> `_`, upper-cased normalization Key Vault names effectively
    force, so a component or config value referencing a secret by name resolves
    identically in both environments without the caller knowing which backend is
    behind it."""
    assert _env_key(name) == expected


async def test_dotenv_provider_reads_the_process_environment(monkeypatch) -> None:
    monkeypatch.setenv("KUBESANDBOX_SECRET_DB_PASSWORD", "from-env")
    provider = DotenvSecretsProvider(dotenv_path=None)
    assert await provider.get("db-password") == "from-env"


async def test_dotenv_provider_falls_back_to_the_dotenv_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KUBESANDBOX_SECRET_DB_PASSWORD", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "IRRELEVANT=x\n"
        'KUBESANDBOX_SECRET_DB_PASSWORD="quoted-value"\n'
    )
    provider = DotenvSecretsProvider(dotenv_path=env_file)
    # One matched layer of quotes is stripped — the only .env quoting convention this
    # local-dev reader honors.
    assert await provider.get("db-password") == "quoted-value"


async def test_process_environment_wins_over_the_dotenv_file(tmp_path, monkeypatch) -> None:
    """Env-first ordering matters: docker-compose/uvicorn invocations inject overrides as
    real env vars, and those must beat a stale `.env` left over on a dev machine."""
    env_file = tmp_path / ".env"
    env_file.write_text("KUBESANDBOX_SECRET_TOKEN=from-file\n")
    monkeypatch.setenv("KUBESANDBOX_SECRET_TOKEN", "from-env")
    provider = DotenvSecretsProvider(dotenv_path=env_file)
    assert await provider.get("token") == "from-env"


async def test_dotenv_provider_raises_a_domain_error_when_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KUBESANDBOX_SECRET_NOPE", raising=False)
    provider = DotenvSecretsProvider(dotenv_path=tmp_path / "does-not-exist.env")
    with pytest.raises(SecretNotFoundError) as excinfo:
        await provider.get("nope")
    # The message names the env var to set — a "not found" with no remedy is the
    # least useful error a config problem can produce.
    assert "KUBESANDBOX_SECRET_NOPE" in str(excinfo.value)


def test_keyvault_provider_constructs_without_azure_credentials() -> None:
    """Constructing it must not touch Azure: the azure imports are deliberately inside
    `get()` so `local` (which never selects this provider) doesn't pay the import cost,
    and so a missing optional dependency can't break an unrelated import."""
    settings = Settings(secrets={"provider": "azure_keyvault", "vault_url": "https://v.vault.azure.net/"})
    provider = AzureKeyVaultSecretsProvider(settings.secrets)
    assert provider._vault_url == "https://v.vault.azure.net/"


# -- doc §9 stubs: they must actually raise -------------------------------------------


@pytest.mark.parametrize(
    ("provider", "expected_fragment"),
    [
        (AWSSecretsProvider(), "AWS Secrets Manager support coming soon"),
        (GCPSecretsProvider(), "GCP Secret Manager support coming soon"),
        (AWSObjectStorageProvider(), "S3/GCS support coming soon"),
        (GCPObjectStorageProvider(), "S3/GCS support coming soon"),
        (AWSImageRegistryProvider(), "ECR/Artifact Registry support coming soon"),
        (GCPImageRegistryProvider(), "ECR/Artifact Registry support coming soon"),
    ],
)
def test_every_stub_carries_the_docs_own_wording(provider, expected_fragment) -> None:
    assert isinstance(provider, ComingSoonProvider)
    assert provider.coming_soon == expected_fragment


async def test_stub_methods_raise_rather_than_silently_no_op() -> None:
    """Doc §9: stubs "fail loudly and immediately (raise, not silent no-op)". A stub that
    returned None would let a build "succeed" having stored nothing."""
    with pytest.raises(NotImplementedError):
        await AWSSecretsProvider().get("anything")
    with pytest.raises(NotImplementedError):
        await AWSObjectStorageProvider().put("k", b"v")
    with pytest.raises(NotImplementedError):
        await AWSObjectStorageProvider().get("k")
    with pytest.raises(NotImplementedError):
        await AWSObjectStorageProvider().delete("k")
    with pytest.raises(NotImplementedError):
        await GCPImageRegistryProvider().push("repo:tag")
    with pytest.raises(NotImplementedError):
        await GCPImageRegistryProvider().resolve("repo:tag")


# -- startup validation ---------------------------------------------------------------


def test_local_defaults_validate_clean() -> None:
    """The whole point of the check is that a correct configuration passes it silently —
    a validator that fired on the default profile would just be turned off."""
    validate_cloud_providers(Settings())


@pytest.mark.parametrize(
    ("overrides", "concern"),
    [
        ({"secrets": {"provider": "aws"}}, "secrets"),
        ({"secrets": {"provider": "gcp"}}, "secrets"),
        ({"object_storage": {"provider": "aws"}}, "object_storage"),
        ({"image_registry": {"provider": "gcp"}}, "image_registry"),
    ],
)
def test_selecting_an_unimplemented_cloud_fails_at_startup(overrides, concern) -> None:
    """Doc §9's actual requirement: caught at startup, not mid-request. The message has
    to name which setting is wrong, or an operator is left guessing between three."""
    settings = Settings(**overrides)
    with pytest.raises(ConfigurationError) as excinfo:
        validate_cloud_providers(settings)
    assert concern in str(excinfo.value)
    assert "coming soon" in str(excinfo.value)


def test_assert_cloud_provider_usable_passes_a_real_provider() -> None:
    assert_cloud_provider_usable(
        LocalImageStore(Settings().image_registry), concern="image_registry", configured="local"
    )


# -- factory dispatch -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "expected_type"),
    [("dotenv", DotenvSecretsProvider), ("aws", AWSSecretsProvider), ("gcp", GCPSecretsProvider)],
)
def test_build_secrets_provider_dispatch(provider, expected_type) -> None:
    settings = Settings(secrets={"provider": provider})
    assert isinstance(build_secrets_provider(settings), expected_type)


def test_build_secrets_provider_dispatches_azure_keyvault() -> None:
    settings = Settings(secrets={"provider": "azure_keyvault", "vault_url": "https://v.vault.azure.net/"})
    assert isinstance(build_secrets_provider(settings), AzureKeyVaultSecretsProvider)


def test_the_other_two_factories_still_dispatch_their_stubs() -> None:
    """Guards the refactor that moved these stubs onto `ComingSoonProvider` — the
    factories still have to hand back an instance for `validate_cloud_providers` to
    inspect, rather than raising during construction."""
    assert isinstance(
        build_object_storage_provider(Settings(object_storage={"provider": "aws"})),
        AWSObjectStorageProvider,
    )
    assert isinstance(
        build_image_registry_provider(Settings(image_registry={"provider": "gcp"})),
        GCPImageRegistryProvider,
    )
