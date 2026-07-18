"""Async Redis client factory (doc §10.1) — session/attach registry, heartbeats, idle
timers, rate limiting, pool-claim locks. First real user is the WS attach gateway's
single-viewer lock (Phase 4, app/streaming/ws_gateway.py); later phases (rate
limiting, pool claims) reuse the same client rather than each growing their own.
"""

from __future__ import annotations

import redis.asyncio as redis

from app.core.config import Settings


def build_redis_client(settings: Settings) -> redis.Redis:
    return redis.from_url(settings.redis.url, decode_responses=True)
