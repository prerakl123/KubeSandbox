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


def test_aks_prod_profile_loads_kubernetes_and_gvisor() -> None:
    out = _run(
        "from app.core.config import Settings; s = Settings(); "
        "print(s.app_env, s.provisioner.backend, s.provisioner.runtime_class, "
        "s.image_registry.provider, s.auth.disabled)",
        {"KUBESANDBOX_APP_ENV": "aks-prod"},
    )
    assert out == "aks-prod kubernetes gvisor acr False"


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
