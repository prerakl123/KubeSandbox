from __future__ import annotations

import asyncio

from app.domain.execution import WeightClass
from app.services.weight_class_scheduler import WeightClassScheduler


async def test_uncapped_scheduler_never_blocks() -> None:
    scheduler = WeightClassScheduler()
    async with scheduler.slot(WeightClass.HEAVY):
        async with scheduler.slot(WeightClass.HEAVY):
            pass  # would deadlock on a real semaphore of size < 2 if capped


async def test_light_and_standard_are_never_capped() -> None:
    scheduler = WeightClassScheduler(heavy_max_concurrent=1)
    async with scheduler.slot(WeightClass.LIGHT):
        async with scheduler.slot(WeightClass.LIGHT):
            async with scheduler.slot(WeightClass.STANDARD):
                pass  # none of these share heavy's semaphore


async def test_heavy_cap_serializes_beyond_the_limit() -> None:
    scheduler = WeightClassScheduler(heavy_max_concurrent=1)
    order: list[str] = []

    async def task(name: str) -> None:
        async with scheduler.slot(WeightClass.HEAVY):
            order.append(f"{name}-start")
            await asyncio.sleep(0.01)
            order.append(f"{name}-end")

    await asyncio.gather(task("a"), task("b"))

    # With a cap of 1, the second task can only start after the first fully finishes —
    # never interleaved as [a-start, b-start, a-end, b-end].
    assert order == ["a-start", "a-end", "b-start", "b-end"] or order == ["b-start", "b-end", "a-start", "a-end"]
