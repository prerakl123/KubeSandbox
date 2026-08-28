"""Config is resolved from module-level state fixed at import time (KUBESANDBOX_APP_ENV
picks the yaml file before Settings even exists — see app/core/config.py), so these
scenarios are exercised in a fresh subprocess each time rather than fighting import
caching / reload semantics in-process."""

from __future__ import annotations

import os
import subprocess
import sys

from tests.conftest import REPO_ROOT


def _run(code: str, env_overrides: dict[str, str]) -> str:
    env = {**os.environ, **env_overrides}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_local_profile_loads_defaults() -> None:
    out = _run(
        "from app.core.config import Settings; s = Settings(); "
        "print(s.app_env, s.provisioner.backend, s.auth.disabled)",
        {"KUBESANDBOX_APP_ENV": "local"},
    )
    assert out == "local docker True"


_PROD_JWT_SECRET = "x" * 32
"""Any 32+ byte value: `Settings` refuses the committed placeholder outside `local`
(Phase 9's guard — see test_prod_refuses_the_placeholder_jwt_secret below), so every
aks-prod scenario has to inject one, exactly as a real deployment does from Key Vault."""


def test_aks_prod_profile_loads_kubernetes_and_gvisor() -> None:
    out = _run(
        "from app.core.config import Settings; s = Settings(); "
        "print(s.app_env, s.provisioner.backend, s.provisioner.runtime_class, "
        "s.image_registry.provider, s.auth.disabled)",
        {"KUBESANDBOX_APP_ENV": "aks-prod", "KUBESANDBOX_AUTH__JWT_SECRET": _PROD_JWT_SECRET},
    )
    assert out == "aks-prod kubernetes gvisor acr False"


def _expect_failure(code: str, env_overrides: dict[str, str]) -> str:
    """Counterpart to `_run` for the guard tests, which assert that Settings *refuses*
    to construct — `_run` asserts a zero exit code, which is the opposite."""
    env = {**os.environ, **env_overrides}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=30,
    )
    assert result.returncode != 0, f"expected failure, got: {result.stdout}"
    return result.stderr


def test_prod_refuses_the_placeholder_jwt_secret() -> None:
    """Shipping the committed default would let anyone who has read this repo mint a
    valid admin session token — a full authentication bypass, so it must be impossible
    to deploy quietly (Phase 9)."""
    stderr = _expect_failure(
        "from app.core.config import Settings; Settings()",
        {"KUBESANDBOX_APP_ENV": "aks-prod"},
    )
    assert "auth.jwt_secret must be overridden" in stderr


def test_prod_refuses_a_too_short_jwt_secret() -> None:
    """RFC 7518 §3.2's floor for HS256 — an HMAC key shorter than the digest is
    brute-forcible offline, and this key mints session tokens."""
    stderr = _expect_failure(
        "from app.core.config import Settings; Settings()",
        {"KUBESANDBOX_APP_ENV": "aks-prod", "KUBESANDBOX_AUTH__JWT_SECRET": "too-short"},
    )
    assert "at least 32 bytes" in stderr


def test_cors_enabled_requires_at_least_one_origin() -> None:
    stderr = _expect_failure(
        "from app.core.config import Settings; Settings()",
        {"KUBESANDBOX_APP_ENV": "local", "KUBESANDBOX_CORS__ENABLED": "true"},
    )
    assert "cors.allow_origins must list at least one origin" in stderr


def test_cors_refuses_wildcard_with_credentials() -> None:
    """Browsers reject this combination outright; failing at startup makes that obvious
    instead of surfacing as an unexplained CORS error in a console."""
    stderr = _expect_failure(
        "from app.core.config import Settings; Settings()",
        {
            "KUBESANDBOX_APP_ENV": "local",
            "KUBESANDBOX_CORS__ENABLED": "true",
            "KUBESANDBOX_CORS__ALLOW_ORIGINS": '["*"]',
            "KUBESANDBOX_CORS__ALLOW_CREDENTIALS": "true",
        },
    )
    assert "cannot contain '*'" in stderr


def test_tracing_enabled_requires_an_otlp_endpoint() -> None:
    """A silently-wrong default endpoint is worse than a startup failure: it produces a
    background retry loop that drops every span."""
    stderr = _expect_failure(
        "from app.core.config import Settings; Settings()",
        {"KUBESANDBOX_APP_ENV": "local", "KUBESANDBOX_OBSERVABILITY__TRACING_ENABLED": "true"},
    )
    assert "observability.otlp_endpoint is required" in stderr


def test_keyvault_secrets_provider_requires_a_vault_url() -> None:
    stderr = _expect_failure(
        "from app.core.config import Settings; Settings()",
        {
            "KUBESANDBOX_APP_ENV": "local",
            "KUBESANDBOX_SECRETS__PROVIDER": "azure_keyvault",
            "KUBESANDBOX_SECRETS__VAULT_URL": "",
        },
    )
    assert "secrets.vault_url is required" in stderr


def test_env_var_overrides_yaml_file() -> None:
    out = _run(
        "from app.core.config import Settings; print(Settings().redis.url)",
        {"KUBESANDBOX_APP_ENV": "local", "KUBESANDBOX_REDIS__URL": "redis://overridden:1234/0"},
    )
    assert out == "redis://overridden:1234/0"


def test_auth_disabled_forbidden_outside_local() -> None:
    env = {**os.environ, "KUBESANDBOX_APP_ENV": "aks-prod", "KUBESANDBOX_AUTH__DISABLED": "true"}
    result = subprocess.run(
        [sys.executable, "-c", "from app.core.config import Settings; Settings()"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=30,
    )
    assert result.returncode != 0
    assert "auth.disabled may only be true when app_env == 'local'" in result.stderr
