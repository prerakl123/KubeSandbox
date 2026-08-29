"""FastAPI dependencies that apply rate limiting (doc §11).

Deliberately a **dependency per route class** rather than global middleware. Middleware
would have to infer a route's cost from its path and method, which is exactly the kind of
string matching that silently misclassifies a new endpoint; declaring
`Depends(rate_limit_execute)` on the expensive routes makes the budget a visible property
of the route. It also means the limiter runs *after* authentication, so it can key on the
resolved principal instead of an IP address — which is the right identity here, since
every caller is authenticated and IPs are shared behind an ingress.

Rejections carry `Retry-After` plus the RFC 9331 `RateLimit-*` headers, so a correct
client backs off by the real window rather than guessing. `RateLimit-Remaining` is set on
*successful* responses too — a client that only learns its budget by being rejected can't
avoid being rejected.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, Response, status

from app.api.deps import Principal, get_current_principal, get_rate_limiter
from app.core.config import get_settings
from app.services import audit_service as audit
from app.services.audit_service import AuditService
from app.services.rate_limiter import RateLimiter, RateLimitResult, RateLimitRule

EXECUTE_BUCKET = "execute"
MUTATION_BUCKET = "mutation"
READ_BUCKET = "read"


def _apply_headers(response: Response, result: RateLimitResult, rule: RateLimitRule) -> None:
    response.headers["RateLimit-Limit"] = str(result.limit)
    response.headers["RateLimit-Remaining"] = str(result.remaining)
    response.headers["RateLimit-Reset"] = str(result.reset_seconds)
    response.headers["RateLimit-Policy"] = rule.as_header


async def _enforce(
    bucket: str,
    rule: RateLimitRule,
    *,
    principal: Principal,
    limiter: RateLimiter,
    response: Response,
    request: Request,
) -> None:
    identity = limiter.identity(principal)
    result = await limiter.check(bucket, identity, rule)
    _apply_headers(response, result, rule)
    if result.allowed:
        return

    # Audited: a tenant hitting a limit repeatedly is either misbehaving or has a broken
    # client, and both are things an operator wants to see without reading access logs.
    # Standalone because this request is about to be rejected — there is no transaction.
    audit_svc: AuditService | None = getattr(request.app.state, "audit_service", None)
    if audit_svc is not None:
        await audit_svc.record_standalone(
            action=audit.DENIED_RATE_LIMIT,
            principal=principal,
            target=request.url.path,
            detail={"bucket": bucket, "limit": rule.limit, "window_seconds": rule.window_seconds},
        )

    raise HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        f"rate limit exceeded: {rule.limit} requests per {rule.window_seconds}s for this identity",
        headers={
            "Retry-After": str(result.reset_seconds),
            "RateLimit-Limit": str(result.limit),
            "RateLimit-Remaining": "0",
            "RateLimit-Reset": str(result.reset_seconds),
            "RateLimit-Policy": rule.as_header,
        },
    )


async def rate_limit_execute(
    request: Request,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    """For anything that provisions or runs a sandbox — the expensive class."""
    settings = get_settings().rate_limit
    await _enforce(
        EXECUTE_BUCKET,
        RateLimitRule(settings.execute_per_minute, 60),
        principal=principal,
        limiter=limiter,
        response=response,
        request=request,
    )


async def rate_limit_mutation(
    request: Request,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    """For writes that don't provision: files, key management, credit requests."""
    settings = get_settings().rate_limit
    await _enforce(
        MUTATION_BUCKET,
        RateLimitRule(settings.mutation_per_minute, 60),
        principal=principal,
        limiter=limiter,
        response=response,
        request=request,
    )


async def rate_limit_read(
    request: Request,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    """For GETs. Applied to the listing endpoints a UI polls, not to `/healthz`/`/readyz`
    (which the kubelet hits on a fixed schedule and must never be throttled) or
    `/metrics` (same, for Prometheus)."""
    settings = get_settings().rate_limit
    await _enforce(
        READ_BUCKET,
        RateLimitRule(settings.read_per_minute, 60),
        principal=principal,
        limiter=limiter,
        response=response,
        request=request,
    )
