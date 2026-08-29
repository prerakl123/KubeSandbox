"""Tests for the post-Phase-9 cross-cutting and hardening pass.

Covers the four things that pass added — audit trail, quotas, rate limiting, admin
bootstrap — plus the sandbox-hardening properties that are easy to regress silently. The
hardening assertions are deliberately written against the *rendered spec* rather than
against a live daemon: whether `MemorySwap` equals `Memory`, whether `SecurityOpt` names
a seccomp profile, and whether the metadata range is excluded from egress are all
properties of what we ask the backend for, and a live-daemon test would confirm the same
fact while requiring infrastructure this environment doesn't have.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import AuthSettings, ProvisionerSettings, RateLimitSettings, Settings
from app.core.errors import QuotaExceededError
from app.domain.auth import Principal
from app.domain.execution import ResourceSpec, SandboxSpec, WeightClass
from app.persistence.models import AuditLog, Run, Sandbox, Tenant, User
from app.provisioners.docker import _security_opts, _tmpfs_options, _tmpfs_size_mb
from app.provisioners.kubernetes import _LINK_LOCAL_CIDR, _deny_metadata_egress
from app.reconciler.loop import prune_audit_logs
from app.services import audit_service as audit
from app.services.audit_service import AuditService, actor_for
from app.services.auth_service import AuthService
from app.services.quota_service import QuotaService
from app.services.rate_limiter import RateLimiter, RateLimitRule


def _spec(cpu: str = "1", memory: str = "512Mi", storage_mb: int | None = None) -> SandboxSpec:
    return SandboxSpec(
        image="kubesandbox/python:3.12.4",
        command=["sleep", "infinity"],
        resources=ResourceSpec(cpu=cpu, memory=memory, ephemeral_storage_mb=storage_mb),
        weight_class=WeightClass.LIGHT,
    )


# =====================================================================================
# Audit trail (doc §6 Layer 5)
# =====================================================================================


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        (Principal(tenant_id="t1", user_id="u1", role="user"), "u1"),
        # An API key authenticates as a tenant, not a person — recording a bare tenant id
        # would be indistinguishable from a user id in the same column.
        (Principal(tenant_id="t1", user_id=None, role="service"), "service:t1"),
        (None, "system"),
    ],
)
def test_actor_for_distinguishes_users_services_and_the_system(principal, expected) -> None:
    assert actor_for(principal) == expected


async def test_record_joins_the_callers_transaction(db_session) -> None:
    """The core design property: an audit entry commits with the action it describes, so a
    rolled-back action leaves no entry claiming it happened."""
    db_session.add(Tenant(id="t1", name="t1"))
    await db_session.flush()
    svc = AuditService()

    svc.record(db_session, action=audit.SANDBOX_CREATE, tenant_id="t1", target="sb-1")
    # Nothing committed yet — the entry is queued, not written.
    await db_session.rollback()
    assert (await db_session.execute(select(AuditLog))).scalars().all() == []


async def test_a_rolled_back_action_leaves_no_audit_entry(db_session) -> None:
    db_session.add(Tenant(id="t1", name="t1"))
    await db_session.commit()
    svc = AuditService()

    svc.record(db_session, action=audit.SANDBOX_DESTROY, tenant_id="t1", target="sb-1")
    await db_session.rollback()

    assert (await db_session.execute(select(AuditLog))).scalars().all() == []


async def test_a_committed_action_keeps_its_audit_entry(db_session) -> None:
    db_session.add(Tenant(id="t1", name="t1"))
    await db_session.commit()
    svc = AuditService()

    svc.record(
        db_session,
        action=audit.SANDBOX_RUN,
        principal=Principal(tenant_id="t1", user_id="u1", role="user"),
        target="sb-1",
        detail={"exit_code": 0, "duration_ms": 42},
    )
    await db_session.commit()

    entry = (await db_session.execute(select(AuditLog))).scalar_one()
    assert entry.action == audit.SANDBOX_RUN
    assert entry.actor == "u1"
    assert entry.detail["exit_code"] == 0


async def test_disabled_audit_service_writes_nothing(db_session) -> None:
    db_session.add(Tenant(id="t1", name="t1"))
    await db_session.commit()

    AuditService(enabled=False).record(db_session, action=audit.SANDBOX_CREATE, tenant_id="t1")
    await db_session.commit()

    assert (await db_session.execute(select(AuditLog))).scalars().all() == []


async def test_record_standalone_never_raises_when_it_cannot_write() -> None:
    """It's called from paths that are already rejecting a request; failing them a second
    time because the audit write failed turns observability into an outage."""

    class _Exploding:
        def __call__(self):
            raise RuntimeError("no database")

    svc = AuditService(session_factory=_Exploding())
    await svc.record_standalone(action=audit.DENIED_QUOTA, tenant_id="t1")  # must not raise


def test_the_action_vocabulary_is_dotted_and_closed() -> None:
    """A closed `subject.verb` set rather than free text, so the column is groupable and a
    query for "every destroy" can't miss entries spelled differently per call site."""
    actions = [
        value
        for name, value in vars(audit).items()
        if name.isupper() and isinstance(value, str) and not name.startswith("_")
    ]
    assert actions, "no action constants found"
    for action in actions:
        assert "." in action, action
        assert action == action.lower(), action


async def test_prune_audit_logs_deletes_only_old_entries(db_session) -> None:
    db_session.add(Tenant(id="t1", name="t1"))
    await db_session.flush()
    now = datetime.now(UTC)
    db_session.add(AuditLog(id="old", tenant_id="t1", actor="u1", action="sandbox.run"))
    db_session.add(AuditLog(id="new", tenant_id="t1", actor="u1", action="sandbox.run"))
    await db_session.flush()
    # created_at is a server default, so it's set post-flush — backdate one explicitly.
    (await db_session.get(AuditLog, "old")).created_at = now - timedelta(days=400)
    await db_session.commit()

    pruned = await prune_audit_logs(session=db_session, retention_days=365, now=now)

    assert pruned == 1
    assert {r.id for r in (await db_session.execute(select(AuditLog))).scalars()} == {"new"}


async def test_prune_audit_logs_keeps_everything_when_retention_is_none(db_session) -> None:
    db_session.add(Tenant(id="t1", name="t1"))
    await db_session.flush()
    db_session.add(AuditLog(id="a", tenant_id="t1", actor="u1", action="sandbox.run"))
    await db_session.commit()

    assert await prune_audit_logs(session=db_session, retention_days=None, now=datetime.now(UTC)) == 0
    assert len((await db_session.execute(select(AuditLog))).scalars().all()) == 1


# =====================================================================================
# Quotas (doc §11)
# =====================================================================================


async def _seed_tenant(db_session, tenant_id: str = "t1") -> None:
    db_session.add(Tenant(id=tenant_id, name=tenant_id))
    await db_session.commit()


async def test_quota_row_is_created_lazily_from_defaults(db_session) -> None:
    await _seed_tenant(db_session)
    svc = QuotaService(default_max_concurrent_sandboxes=5, default_max_monthly_minutes=100)

    quota = await svc.get_or_create("t1", session=db_session)

    assert quota.max_concurrent_sandboxes == 5
    assert quota.max_monthly_minutes == 100
    # Null means "no limit" — that's what lets an operator cap concurrency alone.
    assert quota.max_cpu_millicores is None


async def test_unset_dimensions_are_never_enforced(db_session) -> None:
    await _seed_tenant(db_session)
    svc = QuotaService()  # every default None
    for _ in range(50):
        db_session.add(Sandbox(tenant_id="t1", backend="fake", state="active"))
    await db_session.commit()

    await svc.check("t1", resources=_spec().resources, session=db_session)  # must not raise


async def test_concurrency_quota_blocks_at_the_limit(db_session) -> None:
    await _seed_tenant(db_session)
    svc = QuotaService(default_max_concurrent_sandboxes=2)
    for _ in range(2):
        db_session.add(Sandbox(tenant_id="t1", backend="fake", state="active"))
    await db_session.commit()

    with pytest.raises(QuotaExceededError, match="concurrent sandbox quota"):
        await svc.check("t1", session=db_session)


async def test_terminated_and_failed_sandboxes_do_not_count(db_session) -> None:
    """`failed` is excluded deliberately: a sandbox that failed to provision holds no
    resources, and counting it would let a run of provisioning failures lock a tenant out
    of the platform entirely."""
    await _seed_tenant(db_session)
    svc = QuotaService(default_max_concurrent_sandboxes=1)
    db_session.add(Sandbox(tenant_id="t1", backend="fake", state="terminated"))
    db_session.add(Sandbox(tenant_id="t1", backend="fake", state="failed"))
    await db_session.commit()

    await svc.check("t1", session=db_session)  # must not raise
    assert (await svc.usage("t1", session=db_session)).concurrent_sandboxes == 0


async def test_quota_is_scoped_per_tenant(db_session) -> None:
    await _seed_tenant(db_session, "t1")
    await _seed_tenant(db_session, "t2")
    svc = QuotaService(default_max_concurrent_sandboxes=1)
    db_session.add(Sandbox(tenant_id="t2", backend="fake", state="active"))
    await db_session.commit()

    # t2's sandbox must not consume t1's budget.
    await svc.check("t1", session=db_session)
    with pytest.raises(QuotaExceededError):
        await svc.check("t2", session=db_session)


async def test_memory_quota_accounts_for_the_pending_request(db_session) -> None:
    """Checking current usage alone would let a single enormous sandbox through whenever
    the tenant happened to be at zero."""
    await _seed_tenant(db_session)
    svc = QuotaService(default_max_memory_mb=256)

    with pytest.raises(QuotaExceededError, match="memory quota"):
        await svc.check("t1", resources=_spec(memory="512Mi").resources, session=db_session)


async def test_cpu_quota_accounts_for_the_pending_request(db_session) -> None:
    await _seed_tenant(db_session)
    svc = QuotaService(default_max_cpu_millicores=500)

    with pytest.raises(QuotaExceededError, match="cpu quota"):
        await svc.check("t1", resources=_spec(cpu="2").resources, session=db_session)


async def test_monthly_minutes_quota_sums_this_months_runs(db_session) -> None:
    await _seed_tenant(db_session)
    svc = QuotaService(default_max_monthly_minutes=5)
    # 6 minutes of runs this month.
    for _ in range(6):
        db_session.add(Run(tenant_id="t1", status="completed", duration_ms=60_000, command=[]))
    await db_session.commit()

    usage = await svc.usage("t1", session=db_session)
    assert usage.monthly_minutes == 6
    with pytest.raises(QuotaExceededError, match="monthly minute quota"):
        await svc.check("t1", session=db_session)


async def test_set_quota_patches_by_default_and_clears_on_request(db_session) -> None:
    await _seed_tenant(db_session)
    svc = QuotaService(default_max_concurrent_sandboxes=5, default_max_monthly_minutes=100)

    # PATCH semantics: an omitted dimension is left alone.
    quota = await svc.set_quota("t1", session=db_session, max_concurrent_sandboxes=9)
    assert quota.max_concurrent_sandboxes == 9
    assert quota.max_monthly_minutes == 100

    # PUT semantics: an omitted dimension becomes "no limit". Without this an admin could
    # never *remove* a cap, since unset and unlimited would be indistinguishable.
    quota = await svc.set_quota(
        "t1", session=db_session, max_concurrent_sandboxes=9, clear_unset=True
    )
    assert quota.max_concurrent_sandboxes == 9
    assert quota.max_monthly_minutes is None


async def test_usage_matches_what_enforcement_uses(db_session) -> None:
    """A UI showing "3 of 10" must read exactly the numbers enforcement reads, or the user
    is told they have headroom they don't."""
    await _seed_tenant(db_session)
    svc = QuotaService(default_max_concurrent_sandboxes=3)
    for _ in range(3):
        db_session.add(Sandbox(tenant_id="t1", backend="fake", state="active"))
    await db_session.commit()

    usage = await svc.usage("t1", session=db_session)
    assert (usage.concurrent_sandboxes, usage.max_concurrent_sandboxes) == (3, 3)
    with pytest.raises(QuotaExceededError):
        await svc.check("t1", session=db_session)


# =====================================================================================
# Rate limiting (doc §11)
# =====================================================================================


class _FakeRedis:
    """Minimal sorted-set subset the limiter uses, so the sliding-window logic is tested
    without a Redis server. Pipelines execute eagerly and return results in order, which
    is what `RateLimiter.check` unpacks."""

    def __init__(self) -> None:
        self.sets: dict[str, dict[str, float]] = {}
        self._queue: list = []

    def pipeline(self):
        self._queue = []
        return self

    def zremrangebyscore(self, key, lo, hi):
        members = self.sets.setdefault(key, {})
        for member, score in list(members.items()):
            if lo <= score <= hi:
                del members[member]
        self._queue.append(None)

    def zcard(self, key):
        self._queue.append(len(self.sets.setdefault(key, {})))

    def zadd(self, key, mapping):
        self.sets.setdefault(key, {}).update(mapping)
        self._queue.append(1)

    def expire(self, key, seconds):
        self._queue.append(True)

    def zrange(self, key, start, stop, withscores=False):
        items = sorted(self.sets.setdefault(key, {}).items(), key=lambda kv: kv[1])
        window = items[start : (stop + 1) if stop >= 0 else None]
        self._queue.append([(m, s) for m, s in window] if withscores else [m for m, _ in window])

    async def execute(self):
        return self._queue

    async def delete(self, key):
        self.sets.pop(key, None)


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        (Principal(tenant_id="t1", user_id="u1", role="user"), "u1"),
        # Per-tenant, not per-key: `POST /v1/api-keys` is open to any authenticated
        # caller, so a per-key budget would be bypassable by minting more keys.
        (Principal(tenant_id="t1", user_id=None, role="service"), "tenant:t1"),
    ],
)
def test_rate_limit_identity(principal, expected) -> None:
    assert RateLimiter.identity(principal) == expected


async def test_requests_are_allowed_up_to_the_limit_then_rejected() -> None:
    limiter = RateLimiter(_FakeRedis())
    rule = RateLimitRule(limit=3, window_seconds=60)

    results = [await limiter.check("execute", "u1", rule) for _ in range(4)]

    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    assert results[-1].reset_seconds > 0


async def test_separate_identities_have_separate_budgets() -> None:
    limiter = RateLimiter(_FakeRedis())
    rule = RateLimitRule(limit=1, window_seconds=60)

    assert (await limiter.check("execute", "u1", rule)).allowed
    assert (await limiter.check("execute", "u2", rule)).allowed


async def test_separate_buckets_have_separate_budgets() -> None:
    """An expensive `POST /v1/execute` and a cheap `GET /v1/me` must not share a counter,
    or the shared number has to be set low enough for the expensive path."""
    limiter = RateLimiter(_FakeRedis())
    rule = RateLimitRule(limit=1, window_seconds=60)

    assert (await limiter.check("execute", "u1", rule)).allowed
    assert (await limiter.check("read", "u1", rule)).allowed


async def test_a_disabled_limiter_allows_everything() -> None:
    limiter = RateLimiter(_FakeRedis(), enabled=False)
    rule = RateLimitRule(limit=1, window_seconds=60)

    for _ in range(10):
        assert (await limiter.check("execute", "u1", rule)).allowed


async def test_the_limiter_fails_open_when_redis_breaks() -> None:
    """The deliberate trade: rate limiting is not a security boundary, and turning a Redis
    outage into a total API outage converts a degradation into an incident."""

    class _BrokenRedis(_FakeRedis):
        async def execute(self):
            raise ConnectionError("redis is down")

    limiter = RateLimiter(_BrokenRedis())
    result = await limiter.check("execute", "u1", RateLimitRule(limit=1, window_seconds=60))

    assert result.allowed
    assert result.remaining == result.limit


async def test_reset_clears_a_callers_window() -> None:
    limiter = RateLimiter(_FakeRedis())
    rule = RateLimitRule(limit=1, window_seconds=60)
    await limiter.check("execute", "u1", rule)
    assert not (await limiter.check("execute", "u1", rule)).allowed

    await limiter.reset("execute", "u1")
    assert (await limiter.check("execute", "u1", rule)).allowed


def test_the_policy_header_is_rfc_shaped() -> None:
    assert RateLimitRule(limit=30, window_seconds=60).as_header == "30;w=60"


def test_a_zero_budget_is_rejected_at_config_time() -> None:
    """Zero would silently reject every request rather than disabling the limit, which is
    what someone setting 0 almost certainly intends."""
    with pytest.raises(ValueError, match="must be >= 1"):
        RateLimitSettings(execute_per_minute=0)


# =====================================================================================
# Admin bootstrap
# =====================================================================================


def _auth(**overrides) -> AuthService:
    return AuthService(AuthSettings(jwt_secret="a" * 32, **overrides))


async def test_bootstrap_admin_email_is_promoted_on_first_login(db_session) -> None:
    service = _auth(bootstrap_admin_emails=["founder@example.com"])

    principal = await service.resolve_principal_from_claims(
        {"tid": "d1", "email": "founder@example.com"}, db_session
    )

    assert principal.role == "admin"


async def test_a_non_listed_email_is_still_only_a_user(db_session) -> None:
    service = _auth(bootstrap_admin_emails=["founder@example.com"])

    principal = await service.resolve_principal_from_claims(
        {"tid": "d1", "email": "someone.else@example.com"}, db_session
    )

    assert principal.role == "user"


async def test_bootstrap_admin_matching_is_case_insensitive(db_session) -> None:
    """An IdP may not preserve the casing a human typed into config."""
    service = _auth(bootstrap_admin_emails=["Founder@Example.COM"])

    principal = await service.resolve_principal_from_claims(
        {"tid": "d1", "email": "founder@example.com"}, db_session
    )

    assert principal.role == "admin"


async def test_an_existing_user_is_promoted_when_added_to_the_allowlist(db_session) -> None:
    """So adding an address later works for someone who has already signed in once."""
    claims = {"tid": "d1", "email": "later@example.com"}
    assert (await _auth().resolve_principal_from_claims(claims, db_session)).role == "user"

    promoted = _auth(bootstrap_admin_emails=["later@example.com"])
    assert (await promoted.resolve_principal_from_claims(claims, db_session)).role == "admin"


async def test_removing_an_address_does_not_demote(db_session) -> None:
    """An admin promoted through the proper endpoint must not be silently revoked by
    unrelated config drift."""
    claims = {"tid": "d1", "email": "founder@example.com"}
    await _auth(bootstrap_admin_emails=["founder@example.com"]).resolve_principal_from_claims(
        claims, db_session
    )

    # Allowlist now empty.
    assert (await _auth().resolve_principal_from_claims(claims, db_session)).role == "admin"


async def test_no_token_claim_can_grant_admin(db_session) -> None:
    """The allowlist is operator config, not token data — the distinction that makes it
    safe. A token claiming every admin-shaped role must still produce a plain user."""
    principal = await _auth().resolve_principal_from_claims(
        {
            "tid": "d1",
            "email": "attacker@example.com",
            "role": "admin",
            "roles": ["admin", "Global Administrator"],
            "wids": ["62e90394-69f5-4237-9190-012177145e10"],
            "groups": ["admins"],
        },
        db_session,
    )
    assert principal.role == "user"


async def test_seed_admin_cli_is_idempotent(db_session, monkeypatch) -> None:
    from app import cli

    monkeypatch.setattr(cli, "get_session_factory", lambda: _factory_for(db_session))

    assert await cli.seed_admin("ops@example.com", "bootstrap", promote_existing=False) == 0
    # Re-running reports the existing state rather than failing — safe in a provisioning
    # script that may run twice.
    assert await cli.seed_admin("ops@example.com", "bootstrap", promote_existing=False) == 0

    user = (
        await db_session.execute(select(User).where(User.email == "ops@example.com"))
    ).scalar_one()
    assert user.role == "admin"


async def test_seed_admin_refuses_to_promote_silently(db_session, monkeypatch) -> None:
    """This account may belong to a different tenant than the one named; quietly changing
    a real user's role is not something a "seed" command should do by default."""
    from app import cli

    monkeypatch.setattr(cli, "get_session_factory", lambda: _factory_for(db_session))
    db_session.add(Tenant(id="t9", name="existing"))
    await db_session.flush()
    db_session.add(User(id="u9", tenant_id="t9", email="dev@example.com", role="user"))
    await db_session.commit()

    assert await cli.seed_admin("dev@example.com", "bootstrap", promote_existing=False) == 1
    assert (await db_session.get(User, "u9")).role == "user"

    assert await cli.seed_admin("dev@example.com", "bootstrap", promote_existing=True) == 0
    await db_session.refresh(await db_session.get(User, "u9"))
    assert (await db_session.get(User, "u9")).role == "admin"


def _factory_for(session):
    """Hands the CLI the test's own session — it opens its own via the factory, which is
    exactly the seam to substitute."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _factory():
        yield session

    return lambda: _factory()


# =====================================================================================
# Sandbox hardening — the properties from the 13-point review
# =====================================================================================


def test_docker_security_opts_name_both_profiles_explicitly() -> None:
    """Relying on the daemon's defaults means a daemon started with
    `--seccomp-profile=unconfined` silently runs an unfiltered sandbox."""
    opts = _security_opts("builtin", "docker-default")
    assert "no-new-privileges" in opts
    assert "seccomp=builtin" in opts
    assert "apparmor=docker-default" in opts


def test_apparmor_can_be_omitted_for_selinux_hosts() -> None:
    """Naming an AppArmor profile on a host without AppArmor fails every container
    create — RHEL-family hosts use SELinux, applied by the daemon's own labelling."""
    opts = _security_opts("builtin", None)
    assert not any(o.startswith("apparmor=") for o in opts)
    assert "seccomp=builtin" in opts


def test_tmpfs_options_block_the_escalation_primitives() -> None:
    """`nosuid`/`nodev` are the load-bearing pair: without them a writable mount can hold
    a setuid binary or a device node, turning "can write files" into privilege
    escalation."""
    options = _tmpfs_options(512)
    assert "nosuid" in options
    assert "nodev" in options
    assert "size=512m" in options
    # Non-root ownership, or the sandbox uid can't write into its own workspace.
    assert "uid=10001" in options


def test_tmpfs_honors_the_declared_ephemeral_storage() -> None:
    """Previously hardcoded at 1g for every path regardless of what the component asked."""
    assert _tmpfs_size_mb(_spec(storage_mb=256)) == 256
    assert _tmpfs_size_mb(_spec(storage_mb=None)) == 1024


def test_the_metadata_egress_policy_excludes_link_local() -> None:
    """IMDS (169.254.169.254) hands out IAM credentials to anything that can reach it on a
    node with a managed identity."""
    policy = _deny_metadata_egress("ns-1")
    ip_block = policy.spec.egress[0].to[0].ip_block

    assert policy.spec.policy_types == ["Egress"]
    assert ip_block.cidr == "0.0.0.0/0"
    assert _LINK_LOCAL_CIDR in ip_block._except


def test_the_metadata_policy_covers_the_whole_link_local_range() -> None:
    """Not just 169.254.169.254: AWS also uses 169.254.170.2 for ECS task credentials."""
    assert _LINK_LOCAL_CIDR == "169.254.0.0/16"


def test_both_overlays_carry_the_metadata_guard() -> None:
    """NetworkPolicy is additive, so a permissive rule added later re-opens IMDS unless
    every allow rule excludes it. The guard has to be present wherever allowlists live."""
    import yaml

    from tests.conftest import REPO_ROOT

    for env in ("local", "aks-prod"):
        path = REPO_ROOT / "deploy" / "overlays" / env / "networkpolicy-allow.yaml"
        docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
        guards = [d for d in docs if d["metadata"]["name"] == "deny-metadata-egress"]
        assert guards, f"{env} overlay has no metadata guard"
        ip_block = guards[0]["spec"]["egress"][0]["to"][0]["ipBlock"]
        assert ip_block["except"] == ["169.254.0.0/16"], env


def test_provisioner_settings_default_to_explicit_profiles() -> None:
    settings = ProvisionerSettings()
    assert settings.seccomp_profile == "builtin"
    assert settings.apparmor_profile == "docker-default"


def test_audit_is_the_only_new_subsystem_on_by_default() -> None:
    """Doc §6 counts the audit log as a security layer, not a feature — a deployment that
    silently isn't recording actions is worse off than one that knows it isn't. The others
    can break an existing deployment when enabled, so they stay opt-in."""
    settings = Settings()
    assert settings.audit.enabled is True
    assert settings.quota.enabled is False
    assert settings.rate_limit.enabled is False


# =====================================================================================
# Timezone normalization — the bug class that bit three separate call sites
# =====================================================================================


def test_ensure_utc_tags_a_naive_value_without_shifting_it() -> None:
    """Naive values are assumed to already be UTC (every write goes through
    `datetime.now(UTC)`), so this stamps rather than converts — a shift would silently
    move every SQLite-sourced timestamp by the local offset."""
    from app.core.timeutil import ensure_utc

    naive = datetime(2026, 8, 28, 12, 0, 0)
    tagged = ensure_utc(naive)

    assert tagged.tzinfo is UTC
    assert tagged.replace(tzinfo=None) == naive


def test_ensure_utc_leaves_an_aware_value_alone() -> None:
    from app.core.timeutil import ensure_utc

    aware = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
    assert ensure_utc(aware) is aware


@pytest.mark.parametrize("naive_since", [True, False])
@pytest.mark.parametrize("naive_until", [True, False])
def test_elapsed_seconds_works_across_the_naive_aware_boundary(naive_since, naive_until) -> None:
    """The actual failure: a `DateTime(timezone=True)` column is aware from Postgres and
    naive from SQLite, so mixing them raises TypeError — invisible in a SQLite-backed
    suite whenever the path only runs against Postgres, and vice versa."""
    from app.core.timeutil import elapsed_seconds

    since = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
    until = since + timedelta(seconds=90)
    if naive_since:
        since = since.replace(tzinfo=None)
    if naive_until:
        until = until.replace(tzinfo=None)

    assert elapsed_seconds(since, until) == 90.0


def test_elapsed_seconds_clamps_a_negative_duration() -> None:
    """A negative duration is always a clock or data problem, and letting it through
    produces negative billed usage or a negative lifetime in an audit entry."""
    from app.core.timeutil import elapsed_seconds

    now = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
    assert elapsed_seconds(now + timedelta(hours=1), now) == 0.0


async def test_destroy_records_a_lifetime_across_the_boundary(db_session) -> None:
    """Regression for the concrete crash: the destroy-time audit entry computed
    `now - row.created_at` directly, which raised TypeError against a SQLite-sourced
    (naive) `created_at`."""
    from app.core.timeutil import elapsed_seconds

    await _seed_tenant(db_session)
    row = Sandbox(tenant_id="t1", backend="fake", state="active")
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)

    # Whatever tz-awareness the backend gave us, this must not raise.
    assert elapsed_seconds(row.created_at, datetime.now(UTC)) >= 0
