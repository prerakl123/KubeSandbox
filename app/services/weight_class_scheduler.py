"""Local/Docker-only stand-in for `aks-prod`'s real node-pool segregation (doc §4.3,
§7's own table: "a dedicated AKS node pool + taints/tolerations in aks-prod, a
separate resource budget/queue in local"). A single Docker host has no node pools to
segregate onto, so `heavy` sandboxes are capped to a configured number of concurrent
slots instead — an in-process `asyncio.Semaphore`, correct only because `local` always
runs exactly 1 control-plane replica (doc §7); it cannot and does not attempt to cap
anything cluster-wide, which is exactly why `aks-prod` doesn't use this at all and
relies on real node-pool taints/tolerations (see `app.provisioners.kubernetes`'s
`node_selector`/`tolerations` wiring) instead.

Scoped to `SandboxService.execute()`'s fully self-contained ephemeral lifetime
(acquire -> run -> destroy/release, all in one call) — see that method for why a
longer-lived `create_sandbox()` sandbox isn't wrapped the same way.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.domain.execution import WeightClass


class WeightClassScheduler:
    def __init__(self, *, heavy_max_concurrent: int | None = None) -> None:
        self._heavy_semaphore = asyncio.Semaphore(heavy_max_concurrent) if heavy_max_concurrent else None

    @asynccontextmanager
    async def slot(self, weight_class: WeightClass) -> AsyncIterator[None]:
        semaphore = self._heavy_semaphore if weight_class == WeightClass.HEAVY else None
        if semaphore is None:
            yield
            return
        async with semaphore:
            yield
