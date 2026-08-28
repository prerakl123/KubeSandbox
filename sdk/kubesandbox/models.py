"""Typed response models for the SDK.

Plain `dataclasses`, not pydantic: doc §17 calls this a "thin client SDK", and the
whole point of shipping it as a separate package is that a workflow-builder can install
it without inheriting the control plane's dependency tree. httpx is the only runtime
dependency; adding pydantic would double that for no gain over `from_dict` classmethods
this small.

Every model is lenient about unknown keys — `from_dict` reads only the fields it knows.
A control plane that adds a field to a response must never break an older SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _parse_dt(value: Any) -> datetime | None:
    """FastAPI serializes `datetime` as ISO-8601. `fromisoformat` handles the trailing
    `Z` from Python 3.11 on, which this SDK's floor already exceeds."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class BatchRunResult:
    """One bundled batch result (doc §5.1) — `POST /v1/execute` and
    `POST /v1/sandboxes/{id}/runs` both return exactly this."""

    run_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    timed_out: bool = False
    variables: dict[str, Any] | None = None
    """The program's final top-level scope (doc §5.3), when the language component
    declares `supportsVariableDump` — `None` for languages that don't, and also `None`
    if the dump was absent or unparsable."""

    @property
    def ok(self) -> bool:
        """Ran to completion with exit 0, no timeout, no truncation. Deliberately
        stricter than `exit_code == 0`: a truncated or timed-out run's stdout is
        incomplete, so treating it as a success is how a workflow step silently acts on
        half an answer."""
        return self.exit_code == 0 and not self.timed_out and not self.truncated

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchRunResult:
        return cls(
            run_id=data["run_id"],
            exit_code=data["exit_code"],
            stdout=data["stdout"],
            stderr=data["stderr"],
            duration_ms=data["duration_ms"],
            truncated=data.get("truncated", False),
            timed_out=data.get("timed_out", False),
            variables=data.get("variables"),
        )


@dataclass(frozen=True)
class Sandbox:
    """A sandbox record as returned by `POST/GET /v1/sandboxes` (doc §17)."""

    id: str
    state: str
    backend: str
    weight_class: str
    persistent: bool
    created_at: datetime | None = None
    template_ref: str | None = None
    component_refs: list[str] = field(default_factory=list)
    last_active_at: datetime | None = None
    terminated_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Sandbox:
        return cls(
            id=data["id"],
            state=data["state"],
            backend=data["backend"],
            weight_class=data["weight_class"],
            persistent=data["persistent"],
            created_at=_parse_dt(data.get("created_at")),
            template_ref=data.get("template_ref"),
            component_refs=list(data.get("component_refs") or []),
            last_active_at=_parse_dt(data.get("last_active_at")),
            terminated_at=_parse_dt(data.get("terminated_at")),
        )


@dataclass(frozen=True)
class FileEntry:
    path: str
    is_dir: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileEntry:
        return cls(path=data["path"], is_dir=data["is_dir"])


@dataclass(frozen=True)
class Component:
    """One entitlement-filtered catalog entry (doc §3.6) — what this caller may see and
    select, not the whole registry."""

    key: str
    name: str
    version: str
    category: str
    display_name: str | None = None
    description: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Component:
        return cls(
            key=data["key"],
            name=data["name"],
            version=data["version"],
            category=data["category"],
            # The API field is camelCase (it mirrors the manifest's own YAML key);
            # renamed here because the rest of this SDK is snake_case Python.
            display_name=data.get("displayName"),
            description=data.get("description"),
        )


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    version: str
    base_ref: str
    component_refs: list[str] = field(default_factory=list)
    display_name: str | None = None
    description: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Template:
        return cls(
            key=data["key"],
            name=data["name"],
            version=data["version"],
            base_ref=data["base_ref"],
            component_refs=list(data.get("component_refs") or []),
            display_name=data.get("displayName"),
            description=data.get("description"),
        )


@dataclass(frozen=True)
class Build:
    """A golden-image build (doc §8). Asynchronous by design — `trigger_build` returns
    a `pending` record; poll `get_build` (or use `wait_for_build`)."""

    id: str
    component_name: str
    component_version: str
    strategy: str
    status: str
    image_ref: str | None = None
    artifact_ref: str | None = None
    error: str | None = None
    log_excerpt: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def done(self) -> bool:
        return self.status in ("succeeded", "failed")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Build:
        return cls(
            id=data["id"],
            component_name=data["component_name"],
            component_version=data["component_version"],
            strategy=data["strategy"],
            status=data["status"],
            image_ref=data.get("image_ref"),
            artifact_ref=data.get("artifact_ref"),
            error=data.get("error"),
            log_excerpt=data.get("log_excerpt"),
            created_at=_parse_dt(data.get("created_at")),
            started_at=_parse_dt(data.get("started_at")),
            finished_at=_parse_dt(data.get("finished_at")),
        )


@dataclass(frozen=True)
class CreditRequest:
    """A self-service ask for more credit / spend-cap headroom (doc §13 follow-up) —
    the path forward after a `QuotaExceededError` on create."""

    id: str
    tenant_id: str
    amount: float
    reason: str
    status: str
    review_note: str | None = None
    created_at: datetime | None = None
    reviewed_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CreditRequest:
        return cls(
            id=data["id"],
            tenant_id=data["tenant_id"],
            amount=data["amount"],
            reason=data["reason"],
            status=data["status"],
            review_note=data.get("review_note"),
            created_at=_parse_dt(data.get("created_at")),
            reviewed_at=_parse_dt(data.get("reviewed_at")),
        )


@dataclass(frozen=True)
class Page:
    """Envelope for a paginated list endpoint.

    `total` is the count ignoring the window, which is what lets a caller size a pager
    or know whether to keep going. `items` is left as the raw dicts' parsed models by
    the client method that produced it — see `KubeSandboxClient.list_sandboxes` and
    friends, each of which returns `Page` with `items` already typed.
    """

    items: list
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total

    @classmethod
    def of(cls, data: dict[str, Any], parse) -> Page:
        return cls(
            items=[parse(item) for item in data["items"]],
            total=data["total"],
            limit=data["limit"],
            offset=data["offset"],
        )


@dataclass(frozen=True)
class Principal:
    """Who the caller is, as the server resolved them."""

    tenant_id: str
    user_id: str | None
    role: str
    email: str | None = None

    @property
    def is_service_account(self) -> bool:
        """An API-key caller: tenant-scoped, with no user identity, so nothing
        user-scoped (a persistent workspace, a credit request's requester) applies."""
        return self.role == "service"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Principal:
        return cls(
            tenant_id=data["tenant_id"],
            user_id=data.get("user_id"),
            role=data["role"],
            email=data.get("email"),
        )


@dataclass(frozen=True)
class Features:
    """Which optional subsystems this deployment has enabled.

    Worth checking before rendering or calling anything that depends on one: every flag
    here is opt-in server-side config, and a call into a disabled feature returns a 400,
    not a graceful degradation.
    """

    persistent_workspaces: bool
    billing: bool
    pooling: bool
    interactive_attach: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Features:
        return cls(
            persistent_workspaces=data["persistent_workspaces"],
            billing=data["billing"],
            pooling=data["pooling"],
            interactive_attach=data.get("interactive_attach", True),
        )


@dataclass(frozen=True)
class Identity:
    """`GET /v1/me` — call it once after authenticating and drive behavior from it."""

    principal: Principal
    features: Features
    app_env: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Identity:
        return cls(
            principal=Principal.from_dict(data["principal"]),
            features=Features.from_dict(data["features"]),
            app_env=data["app_env"],
        )


@dataclass(frozen=True)
class RunRecord:
    """A run from history or the async poll target.

    `stdout`/`stderr`/`variables` are populated on the detail endpoint
    (`get_run`) and absent from list results, which omit output bodies on purpose.
    """

    id: str
    status: str
    sandbox_id: str | None = None
    component_ref: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    truncated: bool = False
    timed_out: bool = False
    created_at: datetime | None = None
    finished_at: datetime | None = None
    command: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    variables: dict[str, Any] | None = None
    error: str | None = None

    @property
    def done(self) -> bool:
        return self.status in ("completed", "failed")

    @property
    def ok(self) -> bool:
        """Same strictness as `BatchRunResult.ok`, plus reaching a terminal status at
        all: a `failed` run never produced a result, so it is never ok."""
        return (
            self.status == "completed"
            and self.exit_code == 0
            and not self.timed_out
            and not self.truncated
        )

    def as_result(self) -> BatchRunResult:
        """The same bundled shape a synchronous `execute()` would have returned (doc
        §5.1's promise for the poll path), so a caller can share one code path between
        the sync and async variants."""
        return BatchRunResult(
            run_id=self.id,
            exit_code=self.exit_code if self.exit_code is not None else -1,
            stdout=self.stdout,
            stderr=self.stderr,
            duration_ms=self.duration_ms or 0,
            truncated=self.truncated,
            timed_out=self.timed_out,
            variables=self.variables,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        return cls(
            id=data["id"],
            status=data["status"],
            sandbox_id=data.get("sandbox_id"),
            component_ref=data.get("component_ref"),
            exit_code=data.get("exit_code"),
            duration_ms=data.get("duration_ms"),
            truncated=data.get("truncated", False),
            timed_out=data.get("timed_out", False),
            created_at=_parse_dt(data.get("created_at")),
            finished_at=_parse_dt(data.get("finished_at")),
            command=list(data.get("command") or []),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            variables=data.get("variables"),
            error=data.get("error"),
        )


@dataclass(frozen=True)
class ApiKeySummary:
    """A key as it appears in a listing — never carrying key material."""

    id: str
    label: str | None
    prefix: str | None
    revoked: bool
    created_at: datetime | None = None
    last_used_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApiKeySummary:
        return cls(
            id=data["id"],
            label=data.get("label"),
            prefix=data.get("prefix"),
            revoked=data["revoked"],
            created_at=_parse_dt(data.get("created_at")),
            last_used_at=_parse_dt(data.get("last_used_at")),
        )


@dataclass(frozen=True)
class CreatedApiKey(ApiKeySummary):
    """Creation only. `api_key` is the plaintext key, returned exactly once and
    unrecoverable afterwards — only its hash is stored server-side. Store it now."""

    api_key: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CreatedApiKey:
        base = ApiKeySummary.from_dict(data)
        return cls(**base.__dict__, api_key=data["api_key"])


@dataclass(frozen=True)
class BillingAccount:
    enabled: bool
    mode: str
    currency: str
    month_to_date_cost: float
    spend_cap: float | None = None
    balance: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BillingAccount:
        return cls(
            enabled=data["enabled"],
            mode=data["mode"],
            currency=data["currency"],
            month_to_date_cost=data["month_to_date_cost"],
            spend_cap=data.get("spend_cap"),
            balance=data.get("balance"),
        )


@dataclass(frozen=True)
class UsageRecord:
    id: str
    resource_type: str
    quantity: float
    cost: float
    sandbox_id: str | None = None
    run_id: str | None = None
    recorded_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageRecord:
        return cls(
            id=data["id"],
            resource_type=data["resource_type"],
            quantity=data["quantity"],
            cost=data["cost"],
            sandbox_id=data.get("sandbox_id"),
            run_id=data.get("run_id"),
            recorded_at=_parse_dt(data.get("recorded_at")),
        )


@dataclass(frozen=True)
class Workspace:
    id: str
    state: str
    quota_mb: int
    used_mb: int
    used_percent: float
    last_access_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Workspace:
        return cls(
            id=data["id"],
            state=data["state"],
            quota_mb=data["quota_mb"],
            used_mb=data["used_mb"],
            used_percent=data["used_percent"],
            last_access_at=_parse_dt(data.get("last_access_at")),
            created_at=_parse_dt(data.get("created_at")),
        )


@dataclass(frozen=True)
class WorkspaceStatus:
    """Three distinct states a caller must handle differently: the feature is off here
    (`enabled` false), the caller has none yet (`workspace` None), or here it is."""

    enabled: bool
    workspace: Workspace | None
    retention: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkspaceStatus:
        ws = data.get("workspace")
        return cls(
            enabled=data["enabled"],
            workspace=Workspace.from_dict(ws) if ws else None,
            retention=data.get("retention") or {},
        )
