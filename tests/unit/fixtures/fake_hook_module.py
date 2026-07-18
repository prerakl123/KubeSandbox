"""A minimal real ComponentHook module for test_hooks.py's load_hook() tests — exists
as an actual importable module (not a mock) since load_hook's whole job is a real
dotted-path import."""

from __future__ import annotations


class FakeHook:
    def __init__(self) -> None:
        self.provisioned: list[str] = []
        self.torn_down: list[str] = []

    async def validate(self, ctx) -> None:
        pass

    async def mutate_pod_spec(self, spec, ctx):
        return spec

    async def on_provision(self, sb, ctx) -> None:
        self.provisioned.append(sb.sandbox_id)

    async def on_teardown(self, sb) -> None:
        self.torn_down.append(sb.sandbox_id)


hook = FakeHook()
