"""Tests for `sdk/kubesandbox` (doc §17's client SDK, Phase 9).

Driven against the **real routers** over `httpx.ASGITransport`, not against a mocked
HTTP layer: the whole risk in a client SDK is drifting out of sync with the server's
actual URLs, param names, and response field names, and only a test that goes through
the genuine FastAPI app can catch that. The transport is the SDK's own documented
`transport=` hook, so this exercises the same code path a real socket-backed client
takes, minus the socket.

`sdk/` is on `sys.path` via `[tool.pytest.ini_options] pythonpath = ["sdk"]` — it is a
separately installable package, deliberately not installed into this environment (see
sdk/pyproject.toml for why).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport
from kubesandbox import AsyncKubeSandboxClient, NotFoundError, QuotaExceededError
from kubesandbox.attach import SIGNALS, _ws_url
from kubesandbox.errors import BadRequestError, ConflictError, error_for_status
from kubesandbox.models import BatchRunResult, Build, Sandbox

from app.api.deps import (
    get_build_manager,
    get_current_principal,
    get_entitlement_service,
    get_registry,
    get_registry_service,
    get_sandbox_service,
    get_template_service,
)
from app.domain.auth import Principal
from app.domain.execution import BatchRunResult as ServerBatchRunResult
from app.domain.execution import FileEntry
from app.extensions.loader import Registry
from app.main import app
from app.persistence.db import get_session
from app.services.build_manager import BuildManager
from app.services.entitlement_service import EntitlementService
from app.services.registry_service import RegistryService
from app.services.sandbox_service import SandboxService
from app.services.template_service import TemplateService
from tests.unit.factories import make_component
from tests.unit.fakes import FakeProvisioner

ADMIN = Principal(tenant_id="tenant-a", user_id="user-a", role="admin")


@pytest.fixture
def registry():
    return Registry(
        components={"python@3.12.4": make_component("python", "3.12.4", default_run="echo {file}")},
        templates={},
    )


@pytest.fixture
def provisioner():
    return FakeProvisioner(
        batch_result=ServerBatchRunResult(
            run_id="r1", exit_code=0, stdout="45\n", stderr="", duration_ms=7, variables={"total": 45}
        ),
        files={"/workspace/data/input.txt": b"hello sdk"},
        tree=[FileEntry(path="data", is_dir=True), FileEntry(path="data/input.txt", is_dir=False)],
    )


@pytest.fixture
async def sdk(tmp_path, db_session, registry, provisioner):
    """An `AsyncKubeSandboxClient` wired to the real app in-process.

    The async client (not the sync one) because `ASGITransport` is an *async* transport
    — a sync `httpx.Client` can't mount an ASGI app without a thread-and-loop bridge,
    which is exactly the mix the module docstring of test_api_v1_endpoints.py warns
    about. The sync client shares all its request semantics with the async one via
    `_transport.py`, and its own distinct logic (the `sandbox()` context manager,
    `wait_for_build`'s polling) is covered separately below.
    """
    entitlements = EntitlementService(db_session)
    registry_service = RegistryService(
        registry, db_session, entitlements, components_dir=tmp_path / "components"
    )
    template_service = TemplateService(
        registry, db_session, entitlements, templates_dir=tmp_path / "templates"
    )
    @asynccontextmanager
    async def _factory():
        yield db_session

    sandbox_service = SandboxService(registry, provisioner, session_factory=_factory)

    async def _override_get_session():
        yield db_session

    app.dependency_overrides[get_current_principal] = lambda: ADMIN
    app.dependency_overrides[get_session] = _override_get_session
    # The real lifespan (which populates app.state) doesn't run under ASGITransport, so
    # the two dependencies that read from it are overridden directly.
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_build_manager] = lambda: BuildManager(
        registry, entitlements, None, None, None
    )
    app.dependency_overrides[get_entitlement_service] = lambda: entitlements
    app.dependency_overrides[get_registry_service] = lambda: registry_service
    app.dependency_overrides[get_template_service] = lambda: template_service
    app.dependency_overrides[get_sandbox_service] = lambda: sandbox_service

    client = AsyncKubeSandboxClient(
        "http://test", api_key="test-key", transport=ASGITransport(app=app)
    )
    yield client
    await client.aclose()
    app.dependency_overrides.clear()


# -- batch execution ------------------------------------------------------------------


async def test_execute_returns_a_parsed_bundled_result(sdk) -> None:
    result = await sdk.execute(language="python", code="print(45)", stdin="")

    assert isinstance(result, BatchRunResult)
    assert result.exit_code == 0
    assert result.stdout == "45\n"
    assert result.duration_ms == 7
    assert result.variables == {"total": 45}
    assert result.ok


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "truncated", "expected"),
    [
        (0, False, False, True),
        (1, False, False, False),
        # A timed-out or truncated run has incomplete stdout even at exit 0 — `ok`
        # must not report success for either, or a workflow step acts on half an answer.
        (0, True, False, False),
        (0, False, True, False),
    ],
)
def test_result_ok_is_stricter_than_exit_code(exit_code, timed_out, truncated, expected) -> None:
    result = BatchRunResult(
        run_id="r",
        exit_code=exit_code,
        stdout="",
        stderr="",
        duration_ms=1,
        timed_out=timed_out,
        truncated=truncated,
    )
    assert result.ok is expected


async def test_execute_omits_unset_optional_fields(sdk) -> None:
    """`version` and `template` are mutually exclusive server-side, and sending
    `version: null` alongside a template would trip that validator — so the client must
    omit them rather than send nulls."""
    result = await sdk.execute(language="python", code="print(1)")
    assert result.run_id == "r1"


# -- sandbox lifecycle ----------------------------------------------------------------


async def test_sandbox_lifecycle_round_trip(sdk) -> None:
    created = await sdk.create_sandbox(language="python")
    assert isinstance(created, Sandbox)
    assert created.state == "active"
    assert created.persistent is False
    assert created.component_refs == ["python@3.12.4"]
    assert created.created_at is not None  # ISO-8601 parsed into a real datetime

    fetched = await sdk.get_sandbox(created.id)
    assert fetched.id == created.id

    run = await sdk.run(created.id, code="print(45)")
    assert run.stdout == "45\n"

    await sdk.destroy_sandbox(created.id)
    # Idempotent, like the server-side call it wraps.
    await sdk.destroy_sandbox(created.id)


async def test_async_sandbox_context_manager_always_destroys(sdk) -> None:
    async with sdk.sandbox(language="python") as sb:
        sandbox_id = sb.id
    assert (await sdk.get_sandbox(sandbox_id)).state == "terminated"


async def test_async_sandbox_context_manager_destroys_on_exception(sdk) -> None:
    with pytest.raises(RuntimeError):
        async with sdk.sandbox(language="python") as sb:
            sandbox_id = sb.id
            raise RuntimeError("caller blew up mid-workflow")
    assert (await sdk.get_sandbox(sandbox_id)).state == "terminated"


# -- files ----------------------------------------------------------------------------


async def test_upload_sends_relative_path_and_utf8_body(sdk, provisioner) -> None:
    """`FakeProvisioner.put_files` only records what it was handed (it is not a
    filesystem), which is exactly the assertion that matters here: the SDK must send a
    *workspace-relative* path and a UTF-8 body, since that's the contract
    `put_files()` documents."""
    async with sdk.sandbox(language="python") as sb:
        await sdk.upload_file(sb.id, "data/input.txt", "hello sdk")
    assert provisioner.put_files_calls == [{"data/input.txt": "hello sdk"}]


async def test_download_and_tree_parse_the_response(sdk) -> None:
    """The fake is seeded with an *absolute* in-sandbox path because that's what the
    router resolves a relative `?path=` to before calling the provisioner — so this
    also pins down that the SDK sends the relative form, not an already-absolute one."""
    async with sdk.sandbox(language="python") as sb:
        assert await sdk.download_file(sb.id, "data/input.txt") == b"hello sdk"
        entries = await sdk.list_tree(sb.id)
    assert [(e.path, e.is_dir) for e in entries] == [("data", True), ("data/input.txt", False)]


async def test_upload_rejects_a_path_escaping_the_workspace(sdk) -> None:
    """Enforced server-side; asserted here so a future SDK-side path helper can't
    quietly start pre-normalizing `..` away and hide the rejection."""
    async with sdk.sandbox(language="python") as sb:
        with pytest.raises(BadRequestError) as excinfo:
            await sdk.upload_file(sb.id, "../escape.txt", "nope")
    assert excinfo.value.status_code == 400


# -- catalog --------------------------------------------------------------------------


async def test_list_components_parses_camelcase_display_name(sdk) -> None:
    components = await sdk.list_components()
    assert [c.key for c in components] == ["python@3.12.4"]
    assert components[0].name == "python"
    assert components[0].category == "language"


async def test_list_components_passes_the_category_filter(sdk) -> None:
    assert await sdk.list_components(category="language") != []
    assert await sdk.list_components(category="database") == []


async def test_get_component_versions_returns_the_raw_payload_with_schema(sdk) -> None:
    payload = await sdk.get_component_versions("python")
    assert payload["name"] == "python"
    assert payload["json_schema"]["title"] == "KubeSandbox Component manifest"


async def test_list_templates_on_an_empty_registry(sdk) -> None:
    assert await sdk.list_templates() == []


# -- error mapping --------------------------------------------------------------------


async def test_unknown_sandbox_raises_not_found(sdk) -> None:
    with pytest.raises(NotFoundError) as excinfo:
        await sdk.get_sandbox("does-not-exist")
    assert excinfo.value.status_code == 404
    assert "does-not-exist" in excinfo.value.detail


async def test_unknown_language_raises_not_found(sdk) -> None:
    """`ComponentNotFoundError` is mapped to 404 by `app/main.py`, not 400 — an
    unavailable language is "no such thing in your catalog", which is also what a
    component you simply aren't entitled to looks like (doc §3.6)."""
    with pytest.raises(NotFoundError) as excinfo:
        await sdk.execute(language="cobol", code="DISPLAY 'HI'.")
    assert "cobol" in excinfo.value.detail


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, BadRequestError),
        (404, NotFoundError),
        (409, ConflictError),
        (429, QuotaExceededError),
    ],
)
def test_error_for_status_maps_known_codes(status, expected) -> None:
    assert isinstance(error_for_status(status, "boom"), expected)


def test_error_for_status_still_raises_for_an_unmapped_code() -> None:
    """A 500 or a proxy's 504 has no specific class — it must still be an exception,
    never silently swallowed."""
    error = error_for_status(500, "internal")
    assert error.status_code == 500
    assert "internal" in str(error)


# -- attach helper (no server needed for the URL/protocol surface) ---------------------


def test_ws_url_upgrades_scheme_and_carries_the_api_key() -> None:
    assert (
        _ws_url("https://kubesandbox.internal", "sb-1", "secret")
        == "wss://kubesandbox.internal/v1/sandboxes/sb-1/attach?api_key=secret"
    )
    # http -> ws for local dev, and no query string at all when there's no key
    # (auth.disabled, doc §7's local convenience).
    assert _ws_url("http://localhost:8000/", "sb-1", None) == "ws://localhost:8000/v1/sandboxes/sb-1/attach"


def test_ws_url_rejects_a_non_http_base_url() -> None:
    from kubesandbox.errors import KubeSandboxError

    with pytest.raises(KubeSandboxError):
        _ws_url("kubesandbox.internal", "sb-1", None)


def test_only_pty_deliverable_signals_are_offered() -> None:
    """A PTY delivers signals as terminal control bytes, so SIGKILL/SIGTERM have no
    representation on this transport at all (doc §5.2) — the SDK must not pretend
    otherwise."""
    assert SIGNALS == ("SIGINT", "SIGQUIT", "SIGTSTP")


# -- sync-client-specific logic -------------------------------------------------------


def test_sync_wait_for_build_polls_until_terminal(monkeypatch) -> None:
    from kubesandbox.client import KubeSandboxClient

    client = KubeSandboxClient("http://test")
    statuses = iter(["pending", "running", "succeeded"])

    def _fake_get_build(build_id: str) -> Build:
        return Build(
            id=build_id,
            component_name="jq",
            component_version="1.0",
            strategy="dockerfile",
            status=next(statuses),
        )

    monkeypatch.setattr(client, "get_build", _fake_get_build)
    monkeypatch.setattr("kubesandbox.client.time.sleep", lambda _seconds: None)

    build = client.wait_for_build("b-1", poll_interval=0)
    assert build.status == "succeeded"
    client.close()


def test_sync_wait_for_build_returns_a_failed_build_rather_than_raising(monkeypatch) -> None:
    """A failed build is a legitimate result to inspect (`error`, `log_excerpt`), not
    an exception — only running out of patience raises."""
    from kubesandbox.client import KubeSandboxClient

    client = KubeSandboxClient("http://test")
    monkeypatch.setattr(
        client,
        "get_build",
        lambda build_id: Build(
            id=build_id,
            component_name="jq",
            component_version="1.0",
            strategy="dockerfile",
            status="failed",
            error="kaniko exited 1",
        ),
    )

    build = client.wait_for_build("b-1", poll_interval=0)
    assert build.status == "failed"
    assert build.error == "kaniko exited 1"
    client.close()


def test_sync_wait_for_build_times_out(monkeypatch) -> None:
    from kubesandbox.client import KubeSandboxClient
    from kubesandbox.errors import KubeSandboxError

    client = KubeSandboxClient("http://test")
    monkeypatch.setattr(
        client,
        "get_build",
        lambda build_id: Build(
            id=build_id,
            component_name="jq",
            component_version="1.0",
            strategy="dockerfile",
            status="running",
        ),
    )
    monkeypatch.setattr("kubesandbox.client.time.sleep", lambda _seconds: None)

    with pytest.raises(KubeSandboxError, match="still 'running'"):
        client.wait_for_build("b-1", timeout=0, poll_interval=0)
    client.close()


def test_a_caller_supplied_client_is_not_closed_by_the_sdk() -> None:
    """Closing something this object didn't open would break an httpx.Client shared
    across several SDK instances."""
    import httpx

    from kubesandbox.client import KubeSandboxClient

    shared = httpx.Client(base_url="http://test")
    sdk_client = KubeSandboxClient("http://test", client=shared)
    sdk_client.close()
    assert not shared.is_closed
    shared.close()


def test_api_key_becomes_a_header_on_the_underlying_client() -> None:
    from kubesandbox._transport import API_KEY_HEADER
    from kubesandbox.client import KubeSandboxClient

    client = KubeSandboxClient("http://test", api_key="ks_live_abc")
    assert client._client.headers[API_KEY_HEADER] == "ks_live_abc"
    client.close()


# -- Phase 9 additions: identity, listings, async runs, keys, billing, workspace -------


async def test_me_parses_identity_and_features(sdk) -> None:
    identity = await sdk.me()
    assert identity.principal.tenant_id == "tenant-a"
    assert identity.principal.role == "admin"
    assert identity.principal.is_service_account is False
    # Every flag present, so a caller can gate on any of them without a KeyError.
    assert identity.features.persistent_workspaces in (True, False)
    assert identity.features.interactive_attach is True


async def test_auth_config_needs_no_credential_path(sdk) -> None:
    config = await sdk.auth_config()
    assert "openid" in config["scopes"]
    assert "session_ttl_seconds" in config


async def test_list_sandboxes_returns_a_typed_page(sdk) -> None:
    async with sdk.sandbox(language="python"):
        page = await sdk.list_sandboxes()
    assert page.total >= 1
    assert all(isinstance(item, Sandbox) for item in page.items)
    assert page.limit == 50 and page.offset == 0


async def test_page_has_more_reflects_the_window(sdk) -> None:
    for _ in range(3):
        await sdk.create_sandbox(language="python")
    first = await sdk.list_sandboxes(limit=1)
    assert first.has_more is True
    last = await sdk.list_sandboxes(limit=1, offset=first.total - 1)
    assert last.has_more is False


async def test_async_execute_then_wait_for_run(sdk) -> None:
    """The whole doc §5.1 async contract from the client side: fire, poll, read the same
    bundled result a synchronous call would have returned."""
    run_id = await sdk.execute_async(language="python", code="print(45)")
    record = await sdk.wait_for_run(run_id, poll_interval=0)

    assert record.done
    assert record.status == "completed"
    assert record.ok
    assert record.as_result().stdout == "45\n"
    assert record.as_result().variables == {"total": 45}


async def test_run_record_ok_requires_a_terminal_completed_status() -> None:
    from kubesandbox.models import RunRecord

    # A `failed` run never produced a result, so it is never ok even with exit_code 0.
    assert RunRecord(id="r", status="failed", exit_code=0).ok is False
    assert RunRecord(id="r", status="pending", exit_code=None).ok is False
    assert RunRecord(id="r", status="completed", exit_code=0).ok is True
    assert RunRecord(id="r", status="completed", exit_code=1).ok is False
    assert RunRecord(id="r", status="completed", exit_code=0, timed_out=True).ok is False


async def test_list_runs_and_get_run(sdk) -> None:
    await sdk.execute(language="python", code="print(45)")
    page = await sdk.list_runs()
    assert page.total >= 1

    detail = await sdk.get_run(page.items[0].id)
    # The detail view carries output; the list view deliberately does not.
    assert detail.stdout == "45\n"
    assert page.items[0].stdout == ""


async def test_api_key_lifecycle_through_the_sdk(sdk) -> None:
    created = await sdk.create_api_key("workflow-builder")
    assert created.api_key.startswith("ks_")
    assert created.prefix == created.api_key[:12]

    page = await sdk.list_api_keys()
    assert created.id in {k.id for k in page.items}
    # Key material never appears in a listing model — there is no field for it.
    assert not hasattr(page.items[0], "api_key")

    await sdk.revoke_api_key(created.id)
    revoked = next(k for k in (await sdk.list_api_keys()).items if k.id == created.id)
    assert revoked.revoked is True


async def test_billing_account_and_usage(sdk) -> None:
    account = await sdk.billing_account()
    assert account.mode in ("credit", "payg")
    assert account.month_to_date_cost >= 0

    usage = await sdk.list_usage()
    assert usage.total == 0  # nothing billed in this fixture


async def test_my_workspace_handles_the_not_created_yet_case(sdk) -> None:
    """One of three states a caller must handle; `workspace` is None rather than a 404,
    because "you have none yet" is normal, not an error."""
    status = await sdk.my_workspace()
    assert status.workspace is None
    assert "idle_retention_days" in status.retention


async def test_list_builds_through_the_sdk(sdk) -> None:
    page = await sdk.list_builds()
    assert page.total == 0
    assert page.items == []


async def test_get_template_returns_the_full_shape(sdk, registry) -> None:
    from tests.unit.factories import make_template

    registry.templates["lab@1.0"] = make_template(
        "lab", "1.0", base_ref="python@3.12.4", component_refs=["python@3.12.4"]
    )
    payload = await sdk.get_template("lab")
    assert payload["name"] == "lab"
    assert payload["versions"][0]["cpu"]


def test_sync_and_async_clients_expose_the_same_surface() -> None:
    """The two clients duplicate their method surface by design; this is what stops them
    drifting — an endpoint added to one and forgotten in the other fails here."""
    from kubesandbox.async_client import AsyncKubeSandboxClient as Async
    from kubesandbox.client import KubeSandboxClient as Sync

    def public(cls) -> set[str]:
        return {name for name in dir(cls) if not name.startswith("_")}

    sync_only = public(Sync) - public(Async)
    async_only = public(Async) - public(Sync)
    # `close`/`aclose` and `api_key` are the only legitimate asymmetries.
    assert sync_only == {"close"}, sync_only
    assert async_only == {"aclose", "api_key"}, async_only
