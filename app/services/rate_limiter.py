"""Rate limiting per API key / user (doc §6 Layer 6, doc §11's "Rate limiting per
key/user") — a cross-cutting item nothing had implemented, so until now every endpoint
was unthrottled.

**Redis, not in-process.** Doc §10.1 already lists Redis for "rate limiting" and the
reason is structural: the API is horizontally scaled behind an HPA (doc §7), so an
in-process counter would give each replica its own budget and the effective limit would
be `N x configured` — silently wrong, and worse as you scale up, which is exactly when
you need it.

**Fails open.** If Redis is unreachable the request is allowed, with a warning. That is
the deliberate trade: rate limiting protects against abuse and accidental hammering, and
turning a Redis outage into a total API outage converts a degradation into an incident.
The opposite choice (fail closed) is right for an authorization check and wrong for a
throttle — nothing here is a security boundary; the security boundaries are authn/authz
and the quota service.

**Sliding window, via a sorted set.** A fixed window lets a caller send 2x the limit
across a boundary (all of it at 59.9s, all of it again at 60.1s). A sorted set keyed by
timestamp costs one extra Redis command and doesn't have that edge. The whole check is a
single pipeline round trip.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import redis.asyncio as redis

from app.core.logging import get_logger
from app.domain.auth import Principal

logger = get_logger(__name__)

_KEY_PREFIX = "ratelimit:"


@dataclass(frozen=True)
class RateLimitRule:
    """`limit` requests per `window_seconds`."""

    limit: int
    window_seconds: int

    @property
    def as_header(self) -> str:
        """RFC 9331-style policy string for the `RateLimit-Policy` header, so a client can
        discover the budget without reading our docs."""
        return f"{self.limit};w={self.window_seconds}"


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    """Seconds until the oldest request in the window ages out — i.e. until at least one
    slot frees. This is what a correct client backs off by, and what `Retry-After`
    carries; a fixed guess would have every rejected client retry in lockstep."""


class RateLimiter:
    """Redis-backed sliding-window limiter.

    Scoped by a caller-supplied bucket name so different route classes get different
    budgets from one implementation — an expensive `POST /v1/execute` and a cheap
    `GET /v1/me` should not share a counter.
    """

    def __init__(self, client: redis.Redis | None, *, enabled: bool = True) -> None:
        self._client = client
        self._enabled = enabled and client is not None

    @staticmethod
    def identity(principal: Principal) -> str:
        """What the budget is charged to.

        The *user* where there is one, so several service keys minted by one person don't
        each get a full budget; otherwise the tenant, since an API key authenticates as a
        tenant (doc §11) and per-key budgets would make the limit trivially bypassable by
        minting more keys — `POST /v1/api-keys` is available to any authenticated caller.
        """
        return principal.user_id or f"tenant:{principal.tenant_id}"

    async def check(self, bucket: str, identity: str, rule: RateLimitRule) -> RateLimitResult:
        """Consume one slot. Never raises — see the module docstring on failing open."""
        if not self._enabled or self._client is None:
            return RateLimitResult(True, rule.limit, rule.limit, 0)

        key = f"{_KEY_PREFIX}{bucket}:{identity}"
        now_ms = int(time.time() * 1000)
        window_ms = rule.window_seconds * 1000
        cutoff = now_ms - window_ms

        try:
            pipe = self._client.pipeline()
            # Drop anything older than the window, then count what's left. Counting before
            # adding is what makes the limit inclusive rather than off by one.
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zcard(key)
            # A unique member per request. This was originally `f"{now_ms}-{id(rule)}"`,
            # which is the same string for every call in the same millisecond with the
            # same rule object — so `zadd` *overwrote* instead of adding and the window
            # never grew past one entry. That undercounts worst precisely under load,
            # which is when the limiter is the only thing standing between a runaway
            # client and the cluster. uuid4 is cheap and collision-free.
            pipe.zadd(key, {f"{now_ms}-{uuid.uuid4().hex}": now_ms})
            # TTL so an idle caller's key disappears instead of accumulating forever —
            # without it every identity that ever called leaks a key.
            pipe.expire(key, rule.window_seconds + 1)
            pipe.zrange(key, 0, 0, withscores=True)
            _, count, _, _, oldest = await pipe.execute()
        except Exception as exc:  # noqa: BLE001 — fail open, deliberately
            logger.warning("rate_limit_check_failed", bucket=bucket, error=str(exc))
            return RateLimitResult(True, rule.limit, rule.limit, 0)

        used = int(count)
        if used >= rule.limit:
            # Over budget. The slot just added is left in place on purpose: a caller that
            # keeps hammering while limited keeps pushing its own window forward, which
            # makes ignoring the limit strictly worse than respecting it.
            reset = rule.window_seconds
            if oldest:
                oldest_ms = int(oldest[0][1])
                reset = max(1, int((oldest_ms + window_ms - now_ms) / 1000) + 1)
            return RateLimitResult(False, rule.limit, 0, reset)

        return RateLimitResult(True, rule.limit, rule.limit - used - 1, rule.window_seconds)

    async def reset(self, bucket: str, identity: str) -> None:
        """Clear a caller's window. For tests and for an operator unblocking someone
        without waiting out the window."""
        if not self._enabled or self._client is None:
            return
        await self._client.delete(f"{_KEY_PREFIX}{bucket}:{identity}")
